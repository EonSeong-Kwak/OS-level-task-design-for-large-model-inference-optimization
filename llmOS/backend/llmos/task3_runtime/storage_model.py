from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BandwidthModel:
    nvme_gbps: float = 3.5
    cpu_gpu_gbps: float = 16.0
    gpu_hbm_gbps: float = 800.0

    def transfer_time(self, gigabytes: float, src: str, dst: str) -> float:
        path = {src, dst}
        if path == {"nvme", "cpu"}:
            bw = self.nvme_gbps
        elif path == {"cpu", "gpu"}:
            bw = self.cpu_gpu_gbps
        elif path == {"nvme", "gpu"}:
            bw = min(self.nvme_gbps, self.cpu_gpu_gbps)
        elif path == {"gpu"} or src == dst:
            bw = self.gpu_hbm_gbps
        else:
            bw = min(self.nvme_gbps, self.cpu_gpu_gbps, self.gpu_hbm_gbps)
        return gigabytes / max(bw, 1e-9)
