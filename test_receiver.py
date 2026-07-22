#!/usr/bin/env python3
import unittest

from receiver import should_process_event


class TestEventGate(unittest.TestCase):
    def test_transcript_ready_events_are_processed(self):
        for event in ("meeting.transcribed", "meeting_transcribed",
                      "meeting.summarized", "Transcription completed"):
            with self.subTest(event=event):
                self.assertTrue(should_process_event(event))

    def test_early_lifecycle_events_are_ignored(self):
        for event in ("meeting.bot_joined", "meeting.started", "meeting.ended"):
            with self.subTest(event=event):
                self.assertFalse(should_process_event(event))


if __name__ == "__main__":
    unittest.main()
