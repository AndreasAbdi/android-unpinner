import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from android_unpinner.jdwplib import JDWPClient


class JDWPHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_handshake_explains_debugger_conflict(self):
        reader = Mock()
        reader.readexactly = AsyncMock(
            side_effect=asyncio.IncompleteReadError(b"", 14)
        )
        writer = Mock()
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            with self.assertRaisesRegex(RuntimeError, "Another debugger"):
                await JDWPClient("127.0.0.1", 1234).__aenter__()
        writer.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
