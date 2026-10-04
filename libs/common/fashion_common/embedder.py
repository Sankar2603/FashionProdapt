
import os
from dataclasses import dataclass

DENSE_DIM = 1024  # BGE-M3 dense vector size


@dataclass
class EncodedText:
    dense: list[float]          # 1024 numbers: overall meaning
    sparse_indices: list[int]   # token ids that matter
    sparse_values: list[float]  # how much each of those tokens matters


class Embedder:
    def __init__(
        self,
        model_name: str | None = None,
        device: str | None = None,
        max_length: int | None = None,
        batch_size: int | None = None,
    ):
        # Imported here so modules that only need EncodedText/DENSE_DIM
        # don't pay the cost of importing PyTorch.
        from FlagEmbedding import BGEM3FlagModel

        self.model_name = model_name or os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
        self.device = device or os.getenv("EMBEDDING_DEVICE", "cpu")
        self.max_length = max_length or int(os.getenv("EMBEDDING_MAX_LENGTH", "512"))
        self.batch_size = batch_size or int(os.getenv("EMBEDDING_BATCH_SIZE", "16"))

        # fp16 is faster on a GPU but not supported well on CPU.
        self._model = BGEM3FlagModel(
            self.model_name,
            use_fp16=self.device.startswith("cuda"),
            devices=self.device,
        )

    def encode(self, texts: list[str]) -> list[EncodedText]:
        """Encode a list of texts. Always pass a list, even for one text."""
        if not texts:
            return []
        output = self._model.encode(
            list(texts),
            batch_size=self.batch_size,
            max_length=self.max_length,
            return_dense=True,
            return_sparse=True,
        )
        results = []
        for dense, weights in zip(output["dense_vecs"], output["lexical_weights"]):
            # weights: {"token_id_as_string": weight}
            results.append(
                EncodedText(
                    dense=[float(x) for x in dense],
                    sparse_indices=[int(token_id) for token_id in weights],
                    sparse_values=[float(w) for w in weights.values()],
                )
            )
        return results