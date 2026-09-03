#!/usr/bin/env python3
import unittest

import receiver
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


class TestSpawnHeader(unittest.TestCase):
    def test_header_carries_a_utc_timestamp_and_the_ids(self):
        line = receiver.spawn_header("01ABC", "meeting.transcribed", now=1788457800)
        self.assertEqual(line, "\n===== 2026-09-03T17:50:00Z spawn meeting-id=01ABC event=meeting.transcribed =====\n")
