import unittest
from unittest.mock import Mock, patch

from reader import service
from reader.model import Utterance


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.reader = service.Reader.__new__(service.Reader)
        self.reader.base = Mock()
        self.reader.locked = False
        self.reader.job = None
        self.reader.task = None
        self.reader.feedback_job = None
        self.reader.phase = "idle"
        self.reader.rate = 1.2
        self.reader.pool = Mock()
        self.reader.tray = Mock()
        self.reader.highlight = Mock()
        self.reader.cue = Mock()
        self.reader.notify = Mock()
        self.request = {"point": [10, 10]}

    def test_repeat_replaces_request_in_every_phase(self):
        self.reader.start(self.request)
        for i, phase in enumerate(("finding text", "recognizing text", "preparing audio", "speaking")):
            with self.subTest(phase=phase):
                job, task = self.reader.job, self.reader.task
                self.reader.pool.submit.return_value = Mock()
                self.reader.update(job, phase)
                request = {"point": [20 + i, 10]}
                self.reader.start(request)
                self.assertTrue(job.is_set())
                task.cancel.assert_called_once_with()
                self.assertIsNot(self.reader.job, job)
                self.assertFalse(self.reader.job.is_set())
                self.assertEqual(self.reader.phase, "finding text")
                self.reader.highlight.update.assert_called_with([])
                self.reader.cue.assert_called_with("accepted", self.reader.job)
                self.reader.pool.submit.assert_called_with(self.reader.run, request, self.reader.job)
                self.reader.update(job, "speaking", ["stale highlight"])
                self.assertEqual(self.reader.phase, "finding text")

    def test_stop_clears_playback_without_starting_another_request(self):
        self.reader.start(self.request)
        job = self.reader.job
        self.reader.update(job, "speaking")
        self.reader.stop()

        self.assertTrue(job.is_set())
        self.assertIsNone(self.reader.job)
        self.assertEqual(self.reader.phase, "idle")
        self.reader.pool.submit.assert_called_once()

    def test_stopped_request_cannot_report_failure_after_restart(self):
        self.reader.start(self.request)
        old_job = self.reader.job
        self.reader.stop()
        self.reader.start(self.request)
        new_job = self.reader.job

        self.reader.finish(old_job, service.FAILED)
        self.assertIs(self.reader.job, new_job)
        self.reader.notify.assert_not_called()

        self.reader.finish(new_job, service.EMPTY)
        self.reader.cue.assert_called_with("unavailable", new_job)
        self.reader.notify.assert_called_once_with(*service.EMPTY)
        self.assertEqual(self.reader.phase, "idle")

    def test_highlight_failure_does_not_interrupt_playback(self):
        self.reader.start(self.request)
        reading = Mock(mode="pointer")
        reading.utterances.return_value = iter([Utterance("Read aloud.", ())])
        reading.rectangles.side_effect = RuntimeError("Highlight unavailable")
        voice = self.reader.voice = Mock()

        def play(wav, cancel, refresh):
            refresh()
            refresh()

        voice.play.side_effect = play
        with (
            patch.object(service.access, "resolve", return_value=reading),
            patch.object(service.GLib, "idle_add", side_effect=lambda fn, *args: fn(*args)),
            self.assertLogs(service.LOG, level="ERROR"),
        ):
            self.reader.run(self.request, self.reader.job)

        voice.play.assert_called_once()
        voice.close.assert_not_called()
        reading.rectangles.assert_called_once()
        self.reader.notify.assert_not_called()
        self.assertEqual(self.reader.phase, "idle")

    def test_empty_target_announces_failure_after_ocr_misses(self):
        self.reader.start(self.request)
        self.reader.voice = Mock()
        cancel = self.reader.job
        with (
            patch.object(service.access, "resolve", return_value=None),
            patch.object(service.ocr, "capture", return_value=None) as recognize,
            patch.object(service.GLib, "idle_add", side_effect=lambda fn, *args: fn(*args)),
        ):
            self.reader.run(self.request, cancel)

        recognize.assert_called_once()
        self.reader.voice.queue.assert_called_once_with("No readable text found at the pointer.", 1.2)
        self.reader.voice.play.assert_called_once()
        self.reader.notify.assert_called_once_with(*service.EMPTY)
        self.reader.cue.assert_called_once_with("accepted", cancel)
        self.assertEqual(self.reader.phase, "idle")

    def test_cancelled_ocr_does_not_announce_failure(self):
        self.reader.start(self.request)
        self.reader.voice = Mock()
        cancel = self.reader.job

        def stop_while_recognizing(*_):
            self.reader.stop()
            return None

        with (
            patch.object(service.access, "resolve", return_value=None),
            patch.object(service.ocr, "capture", side_effect=stop_while_recognizing),
            patch.object(service.GLib, "idle_add", side_effect=lambda fn, *args: fn(*args)),
        ):
            self.reader.run(self.request, cancel)

        self.reader.voice.play.assert_not_called()
        self.reader.notify.assert_not_called()
