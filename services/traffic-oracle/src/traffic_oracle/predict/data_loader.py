"""DuckDB-backed training data loader for DSTGAT.

Reads historical traffic observations via DuckDB's postgres_scanner extension,
builds sliding temporal windows and adjacency matrices, and produces
PyTorch-ready TensorDatasets for DSTGAT training.

Peak memory ~800MB during load (subsample rate controls this).
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import numpy as np
import structlog
import torch
from torch.utils.data import TensorDataset

from traffic_oracle.anomaly.feature_builder import FeatureBuilder
from traffic_oracle.predict.nn.dstgat import DSTGATConfig
from traffic_oracle.runtime.memory_profiler import profile_memory

logger = structlog.get_logger(__name__)

HORIZON_STEPS = [1, 2, 3, 6]  # T+5, T+10, T+15, T+30 at 5-min resolution


@dataclass
class TrainingWindow:
    """A single training sample: input features + target values."""

    node_features: np.ndarray  # (N, T, 8)
    adjacency: np.ndarray  # (N, N)
    targets: np.ndarray  # (N, H, 2) — speed + congestion per horizon
    node_mask: np.ndarray  # (N,) — True for padding nodes
    segment_ids: list[int]


class TrafficDataLoader:
    """Loads and prepares training data for DSTGAT from PostgreSQL via DuckDB."""

    def __init__(
        self,
        database_url: str,
        config: DSTGATConfig | None = None,
    ) -> None:
        self._db_url = database_url
        self._cfg = config or DSTGATConfig()

    @profile_memory
    def load_training_windows(
        self,
        days_back: int = 30,
        sample_rate: float = 0.2,
    ) -> list[TrainingWindow]:
        """Load historical data and build sliding-window training samples.

        Args:
            days_back: How many days of history to load
            sample_rate: Fraction of windows to keep (memory control)

        Returns:
            List of TrainingWindow samples
        """
        con = duckdb.connect()
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute(f"ATTACH '{self._db_url}' AS pg (TYPE postgres, READ_ONLY);")

        # Load observations
        obs_df = con.execute(f"""
            SELECT segment_id, timestamp_utc, speed_kmh, congestion_idx
            FROM pg.traffic.observations
            WHERE timestamp_utc >= now() - INTERVAL '{days_back} days'
              AND data_quality = 'real'
            ORDER BY segment_id, timestamp_utc
        """).fetchdf()

        # Load adjacency
        adj_rows = con.execute("""
            SELECT from_segment_id, to_segment_id
            FROM pg.corridor.segment_adjacency
        """).fetchdf()

        con.close()

        if obs_df.empty:
            logger.warning("data_loader.no_observations", days_back=days_back)
            return []

        segment_ids = sorted(obs_df["segment_id"].unique().tolist())
        seg_to_idx = {sid: i for i, sid in enumerate(segment_ids)}
        N = min(len(segment_ids), self._cfg.max_nodes)
        segment_ids = segment_ids[:N]
        seg_to_idx = {sid: i for i, sid in enumerate(segment_ids)}

        # Build adjacency matrix
        adjacency = np.zeros((N, N), dtype=np.float32)
        for _, row in adj_rows.iterrows():
            src, tgt = int(row["from_segment_id"]), int(row["to_segment_id"])
            if src in seg_to_idx and tgt in seg_to_idx:
                adjacency[seg_to_idx[src], seg_to_idx[tgt]] = 1.0

        # Bin into 5-minute intervals and build feature tensor
        obs_df = obs_df[obs_df["segment_id"].isin(set(segment_ids))]
        obs_df["time_bin"] = obs_df["timestamp_utc"].dt.floor("5min")
        time_bins = sorted(obs_df["time_bin"].unique())

        T = self._cfg.lookback_steps
        max_horizon = max(HORIZON_STEPS)
        windows: list[TrainingWindow] = []

        # Pivot: per (time_bin, segment) → speed, congestion
        speed_matrix = np.full((len(time_bins), N), np.nan, dtype=np.float32)
        cong_matrix = np.full((len(time_bins), N), np.nan, dtype=np.float32)
        bin_to_idx = {b: i for i, b in enumerate(time_bins)}

        for _, row in obs_df.iterrows():
            seg_idx = seg_to_idx.get(int(row["segment_id"]))
            bin_idx = bin_to_idx.get(row["time_bin"])
            if seg_idx is not None and bin_idx is not None:
                speed_matrix[bin_idx, seg_idx] = row["speed_kmh"]
                cong_matrix[bin_idx, seg_idx] = row["congestion_idx"]

        # Forward-fill NaN gaps per segment
        for j in range(N):
            last_speed, last_cong = 60.0, 0.0
            for i in range(len(time_bins)):
                if np.isnan(speed_matrix[i, j]):
                    speed_matrix[i, j] = last_speed
                    cong_matrix[i, j] = last_cong
                else:
                    last_speed = speed_matrix[i, j]
                    last_cong = cong_matrix[i, j]

        # Build sliding windows
        rng = np.random.default_rng(42)
        for t_start in range(len(time_bins) - T - max_horizon):
            if rng.random() > sample_rate:
                continue

            input_features = np.zeros((N, T, 8), dtype=np.float32)
            for t_off in range(T):
                ti = t_start + t_off
                for j in range(N):
                    input_features[j, t_off, 0] = speed_matrix[ti, j] / 60.0
                    input_features[j, t_off, 1] = cong_matrix[ti, j]
                    # Simplified cyclic features from time bin index
                    hour = (ti * 5 / 60) % 24
                    dow = (ti * 5 / 1440) % 7
                    input_features[j, t_off, 2] = np.sin(2 * np.pi * hour / 24)
                    input_features[j, t_off, 3] = np.cos(2 * np.pi * hour / 24)
                    input_features[j, t_off, 4] = np.sin(2 * np.pi * dow / 7)
                    input_features[j, t_off, 5] = np.cos(2 * np.pi * dow / 7)
                    # Speed delta
                    if t_off > 0:
                        input_features[j, t_off, 6] = (
                            speed_matrix[ti, j] - speed_matrix[ti - 1, j]
                        )
                    # Neighbor speed ratio
                    nbr_mask = adjacency[j] > 0
                    if nbr_mask.any():
                        input_features[j, t_off, 7] = (
                            speed_matrix[ti, j]
                            / max(speed_matrix[ti, nbr_mask].mean(), 1e-6)
                        )
                    else:
                        input_features[j, t_off, 7] = 1.0

            # Targets at each horizon
            targets = np.zeros((N, len(HORIZON_STEPS), 2), dtype=np.float32)
            for hi, h_step in enumerate(HORIZON_STEPS):
                ti = t_start + T + h_step - 1
                if ti < len(time_bins):
                    targets[:, hi, 0] = speed_matrix[ti, :N]
                    targets[:, hi, 1] = cong_matrix[ti, :N]

            node_mask = np.zeros(N, dtype=bool)
            windows.append(
                TrainingWindow(
                    node_features=input_features,
                    adjacency=adjacency,
                    targets=targets,
                    node_mask=node_mask,
                    segment_ids=segment_ids,
                )
            )

        logger.info(
            "data_loader.windows_built",
            total_windows=len(windows),
            segments=N,
            time_bins=len(time_bins),
            days_back=days_back,
        )
        return windows

    @staticmethod
    def build_torch_dataset(
        windows: list[TrainingWindow],
        max_nodes: int = 200,
    ) -> TensorDataset:
        """Convert training windows to a PyTorch TensorDataset.

        Pads to max_nodes if needed.

        Returns:
            TensorDataset of (node_features, adjacency, node_mask, targets)
        """
        all_features, all_adj, all_masks, all_targets = [], [], [], []

        for w in windows:
            N = w.node_features.shape[0]
            pad = max_nodes - N

            feat = np.pad(w.node_features, ((0, pad), (0, 0), (0, 0)))
            adj = np.pad(w.adjacency, ((0, pad), (0, pad)))
            mask = np.concatenate([w.node_mask, np.ones(pad, dtype=bool)])
            tgt = np.pad(w.targets, ((0, pad), (0, 0), (0, 0)))

            all_features.append(feat)
            all_adj.append(adj)
            all_masks.append(mask)
            all_targets.append(tgt)

        return TensorDataset(
            torch.from_numpy(np.stack(all_features)),
            torch.from_numpy(np.stack(all_adj)),
            torch.from_numpy(np.stack(all_masks)),
            torch.from_numpy(np.stack(all_targets)),
        )
