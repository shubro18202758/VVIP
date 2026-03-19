// ============================================================================
// reduce_stats.wgsl
// Parallel reduction compute shader for aggregate simulation statistics.
//
// Outputs:
//   stats[0] = sum of speeds              (divide by vehicleCount for average)
//   stats[1] = count of stopped vehicles   (speed < 0.1 m/s)
//   stats[2] = max queue length found      (contiguous stopped vehicles on any segment)
//   stats[3] = total vehicle count processed
//
// Strategy:
//   - Each workgroup reduces up to 256 vehicles using shared memory.
//   - Final partial sums are atomically accumulated into the output buffer.
// ============================================================================

struct Vehicle {
  posX:       f32,
  posY:       f32,
  velX:       f32,
  velY:       f32,
  accelX:     f32,
  accelY:     f32,
  laneOffset: f32,
  flags:      f32,
};

struct SimMeta {
  vehicleCount: u32,
  segmentCount: u32,
  simTime:      f32,
  pad:          f32,
};

// stats buffer layout (8 x u32 = 32 bytes):
//   [0] totalSpeedFixed  – sum of speeds in fixed-point (x1000)
//   [1] stoppedCount
//   [2] maxQueueLength
//   [3] processedCount
//   [4..7] reserved

@group(0) @binding(0) var<storage, read>       vehicles: array<Vehicle>;
@group(0) @binding(1) var<storage, read_write>  stats:    array<atomic<u32>>;
@group(0) @binding(2) var<uniform>              meta:     SimMeta;

// Shared memory for tree reduction within the workgroup
var<workgroup> sharedSpeed:   array<u32, 256>;
var<workgroup> sharedStopped: array<u32, 256>;
var<workgroup> sharedQueue:   array<u32, 256>;

@compute @workgroup_size(256)
fn reduce_main(
  @builtin(global_invocation_id)  gid: vec3<u32>,
  @builtin(local_invocation_id)   lid: vec3<u32>,
  @builtin(workgroup_id)          wid: vec3<u32>,
) {
  let idx = gid.x;
  let localIdx = lid.x;

  // ---------- Load phase ----------
  var spd: f32 = 0.0;
  var stopped: u32 = 0u;
  var queueContrib: u32 = 0u;

  if (idx < meta.vehicleCount) {
    let veh = vehicles[idx];
    spd = sqrt(veh.velX * veh.velX + veh.velY * veh.velY);

    if (spd < 0.1) {
      stopped = 1u;
    }

    // Queue estimation heuristic:
    // If this vehicle AND the next vehicle (idx+1) are both stopped,
    // count as queue=1 for this pair. The max across all threads
    // gives a lower-bound queue length per workgroup.
    // A more accurate per-segment queue would require sorting by segment,
    // but this is a fast approximation for the stats overlay.
    if (stopped == 1u && (idx + 1u) < meta.vehicleCount) {
      let next = vehicles[idx + 1u];
      let nextSpd = sqrt(next.velX * next.velX + next.velY * next.velY);
      if (nextSpd < 0.1) {
        queueContrib = 2u; // at least 2 contiguous stopped
      } else {
        queueContrib = 1u;
      }
    } else if (stopped == 1u) {
      queueContrib = 1u;
    }
  }

  // Store speed as fixed-point (x1000) to use integer shared memory
  sharedSpeed[localIdx]   = u32(spd * 1000.0);
  sharedStopped[localIdx] = stopped;
  sharedQueue[localIdx]   = queueContrib;

  workgroupBarrier();

  // ---------- Tree reduction ----------
  // Sum for speed and stopped; max for queue
  var stride: u32 = 128u;
  loop {
    if (stride == 0u) {
      break;
    }
    if (localIdx < stride) {
      sharedSpeed[localIdx]   = sharedSpeed[localIdx] + sharedSpeed[localIdx + stride];
      sharedStopped[localIdx] = sharedStopped[localIdx] + sharedStopped[localIdx + stride];
      sharedQueue[localIdx]   = max(sharedQueue[localIdx], sharedQueue[localIdx + stride]);
    }
    workgroupBarrier();
    stride = stride >> 1u;
  }

  // ---------- Write partial results (workgroup leader) ----------
  if (localIdx == 0u) {
    // Atomic add partial sums to global stats buffer
    atomicAdd(&stats[0], sharedSpeed[0]);
    atomicAdd(&stats[1], sharedStopped[0]);
    // For max queue, use atomicMax
    atomicMax(&stats[2], sharedQueue[0]);
    // Count of processed vehicles in this workgroup
    let processed = min(256u, meta.vehicleCount - wid.x * 256u);
    atomicAdd(&stats[3], processed);
  }
}

// ===========================================================================
// Clear stats kernel – run before reduce_main to zero the stats buffer
// ===========================================================================
@compute @workgroup_size(1)
fn clear_stats() {
  atomicStore(&stats[0], 0u);
  atomicStore(&stats[1], 0u);
  atomicStore(&stats[2], 0u);
  atomicStore(&stats[3], 0u);
  atomicStore(&stats[4], 0u);
  atomicStore(&stats[5], 0u);
  atomicStore(&stats[6], 0u);
  atomicStore(&stats[7], 0u);
}
