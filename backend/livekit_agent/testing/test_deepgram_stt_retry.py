"""Focused, dependency-free tests for deepgram_stt.open_stream()'s bounded connection retry.

Uses only Python's stdlib `unittest`/`unittest.mock` - `backend/.venv` has no `pytest` installed, and
none was added for this (see the project's own "do not add a new dependency" constraint). Never
touches the network: `websockets.asyncio.client.connect` is mocked at every call site.

Run with:
    cd backend && .venv/bin/python -m unittest livekit_agent.testing.test_deepgram_stt_retry -v
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from livekit_agent import deepgram_stt


class OpenStreamRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_attempt_success_no_retry(self) -> None:
        """A. First connection succeeds: only one attempt, no retry."""
        fake_socket = AsyncMock()
        with (
            patch.object(deepgram_stt, "connect", AsyncMock(return_value=fake_socket)) as mock_connect,
            patch.object(deepgram_stt.asyncio, "sleep", AsyncMock()) as mock_sleep,
        ):
            stream = await deepgram_stt.open_stream("fake-key", "nova-3", 16000)

        self.assertIsInstance(stream, deepgram_stt.SttStream)
        self.assertEqual(mock_connect.call_count, 1)
        mock_sleep.assert_not_called()

    async def test_first_fails_second_succeeds(self) -> None:
        """B. First connection fails, second succeeds: exactly two attempts, one backoff, a
        successful stream is returned - the caller never sees the first failure. Also verifies
        the existing failure warning and the new success log report the correct attempt numbers,
        and that neither log line leaks the api key."""
        fake_socket = AsyncMock()
        with (
            patch.object(
                deepgram_stt, "connect", AsyncMock(side_effect=[TimeoutError(), fake_socket])
            ) as mock_connect,
            patch.object(deepgram_stt.asyncio, "sleep", AsyncMock()) as mock_sleep,
            self.assertLogs(deepgram_stt.log, level="INFO") as logs,
        ):
            stream = await deepgram_stt.open_stream("fake-key", "nova-3", 16000)

        self.assertIsInstance(stream, deepgram_stt.SttStream)
        self.assertEqual(mock_connect.call_count, 2)
        mock_sleep.assert_awaited_once_with(deepgram_stt._RETRY_BACKOFF_SECONDS)

        warning_lines = [line for line in logs.output if "WARNING" in line]
        info_lines = [line for line in logs.output if "INFO" in line]

        self.assertTrue(
            any("attempt 1/2 failed (TimeoutError)" in line for line in warning_lines),
            warning_lines,
        )
        self.assertTrue(
            any("attempt 2/2 succeeded in" in line for line in info_lines),
            info_lines,
        )
        self.assertTrue(all("fake-key" not in line for line in logs.output))

    async def test_both_attempts_fail_raises(self) -> None:
        """C. Both attempts fail: exactly two attempts, DeepgramSttError is raised."""
        with (
            patch.object(
                deepgram_stt,
                "connect",
                AsyncMock(side_effect=[TimeoutError(), ConnectionRefusedError()]),
            ) as mock_connect,
            patch.object(deepgram_stt.asyncio, "sleep", AsyncMock()),
        ):
            with self.assertRaises(deepgram_stt.DeepgramSttError):
                await deepgram_stt.open_stream("fake-key", "nova-3", 16000)

        self.assertEqual(mock_connect.call_count, 2)


if __name__ == "__main__":
    unittest.main()
