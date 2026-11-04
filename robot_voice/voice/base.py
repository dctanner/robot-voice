from typing import Protocol


class VoiceProvider(Protocol):
    native_full_duplex: bool
    supports_images: bool
    requires_paced_audio: bool

    def begin_close(self) -> None: ...
    async def audio(self, pcm: bytes) -> None: ...
    async def close(self) -> bool: ...
