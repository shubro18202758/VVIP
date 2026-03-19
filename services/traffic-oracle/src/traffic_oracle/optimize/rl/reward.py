"""Reward function for convoy signal corridor optimization.

Reward design balances convoy throughput against public traffic disruption.
Typical range: [-100, +20].
"""

from __future__ import annotations


def compute_signal_reward(
    convoy_delay_sec: float,
    public_delay_vehicle_sec: float,
    max_queue_length_m: float,
    green_wave_continuity: float,
    red_light_violations: int,
    convoy_stopped: bool,
) -> float:
    """Compute scalar reward for a single environment step.

    Args:
        convoy_delay_sec: Seconds the convoy was delayed this step (≥0).
        public_delay_vehicle_sec: Total vehicle-seconds of delay imposed
            on public traffic across all signals.
        max_queue_length_m: Longest queue at any signal this step (meters).
        green_wave_continuity: Fraction [0,1] of signals showing green
            as convoy traverses them.
        red_light_violations: Number of signals the convoy hit red.
        convoy_stopped: Whether the convoy came to a full stop.

    Returns:
        Scalar reward, higher is better.
    """
    r = 0.0

    # Heavy penalty for convoy delay — primary objective
    r -= 2.0 * convoy_delay_sec

    # Moderate penalty for public traffic delay
    r -= 0.1 * public_delay_vehicle_sec

    # Penalize excessive queuing beyond 200m threshold
    r -= 0.5 * max(0.0, max_queue_length_m - 200.0)

    # Bonus for maintaining green wave
    r += 1.0 * green_wave_continuity

    # Large penalty for each red-light violation
    r -= 5.0 * red_light_violations * 10.0

    # Severe penalty if convoy stops completely
    if convoy_stopped:
        r -= 50.0

    return r
