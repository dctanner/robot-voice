"""Optional user-supplied licensed acoustic model. No bundled 'Hey Spot' model."""

import numpy as np


class KeywordWake:
    def __init__(self, path, threshold=0.6):
        if path is None or not path.is_file():
            raise ValueError("keyword mode requires a user-supplied keyword_model file")
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise RuntimeError("Install robot-voice[keyword] and supply a licensed model") from exc
        self.model = Model(wakeword_models=[str(path)], inference_framework="onnx")
        self.pending = bytearray()
        self.threshold = threshold

    def reset(self):
        self.pending.clear()
        self.model.reset()

    def feed(self, frame):
        self.pending.extend(frame.pcm)
        if len(self.pending) < 2560:
            return False
        data = bytes(self.pending[:2560])
        del self.pending[:2560]
        predictions = self.model.predict(np.frombuffer(data, dtype="<i2"))
        detected = any(float(score) >= self.threshold for score in predictions.values())
        if detected:
            self.reset()
        return detected
