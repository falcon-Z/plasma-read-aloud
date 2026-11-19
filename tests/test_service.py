import unittest
from unittest.mock import Mock, patch

from reader import service
from reader.model import Utterance


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.reader = service.Reader.__new__(service.Reader)
        self.reader.locked = False
        self.reader.job = None
        self.reader.phase = "idle"
        self.reader.rate = 1.2
        self.reader.pool = Mock()
        self.reader.tray = Mock()
        self.reader.highlight = Mock()
        self.reader.cue = Mock()
        self.reader.notify = Mock()
        self.request = {"point": [10, 10]}

    def test_repeat_keeps_pending_request_and_stops_speech(self):
        self.reader.start(self.request)
        job = self.reader.job
        for phase in ("finding text", "recognizing text", "preparing audio"):
            with self.subTest(phase=phase):
                self.reader.update(job, phase)
                self.reader.start(self.request)
                self.assertIs(self.reader.job, job)
                self.assertFalse(job.is_set())
        self.reader.cue.assert_called_once_with("accepted", job)
        self.reader.pool.submit.assert_called_once_with(self.reader.run, self.request, job)

        self.reader.update(job, "speaking")
        self.reader.start(self.request)
        self.assertTrue(job.is_set())
        self.assertIsNone(self.reader.job)
        self.assertEqual(self.reader.phase, "idle")

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
