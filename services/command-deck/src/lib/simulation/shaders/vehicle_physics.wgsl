// ============================================================================
// vehicle_physics.wgsl
// Intelligent Driver Model (IDM) + simplified MOBIL lane-change compute shaders
// Each thread processes one vehicle. Workgroup size: 256.
// ============================================================================

// ---------------------------------------------------------------------------
// Data structures
// ---------------------------------------------------------------------------

// Vehicle state: 32 bytes (8 x f32)
// Layout per vehicle in the storage buffer:
//   [0] posX        – world X in meters
//   [1] posY        – world Y in meters
//   [2] velX        – longitudinal velocity (m/s)
//   [3] velY        – lateral velocity (m/s)
//   [4] accelX      – longitudinal acceleration (m/s^2)
//   [5] accelY      – lateral acceleration (m/s^2)
//   [6] laneOffset  – lateral offset from road center (m)
//   [7] flags       – bitfield: bit0=isConvoy, bit1=isDiverted,
//                     bit2=isStopped, bit3=isEmergency

struct Vehicle {
  posX:       f32,
  posY:       f32,
  velX:       f32,
  velY:       f32,
  accelX:     f32,
  accelY:     f32,
  laneOffset: f32,
  flags:      f32,   // reinterpret as u32 via bitcast
};

// Road segment: 48 bytes (12 x f32)
struct RoadSegment {
  startX:        f32,
  startY:        f32,
  endX:          f32,
  endY:          f32,
  speedLimitMs:  f32,
  length:        f32,
  lanes:         f32,  // reinterpret as u32
  congestionIdx: f32,  // 0..1
  signalState:   f32,  // 0=red, 1=green, 2=yellow
  closedLanes:   f32,  // bitmask
  pad0:          f32,
  pad1:          f32,
};

// IDM parameters (uniform)
struct IDMParams {
  desiredSpeed:   f32,  // v0  – 13.9 m/s (50 km/h)
  timeHeadway:    f32,  // T   – 1.5 s
  minGap:         f32,  // s0  – 2.0 m
  maxAccel:       f32,  // a   – 2.0 m/s^2
  comfortDecel:   f32,  // b   – 3.0 m/s^2
  delta:          f32,  // delta – 4.0
  vehicleLength:  f32,  // L   – 4.5 m
  dt:             f32,  // simulation timestep (s)
};

// Simulation metadata
struct SimMeta {
  vehicleCount: u32,
  segmentCount: u32,
  simTime:      f32,
  pad:          f32,
};

// ---------------------------------------------------------------------------
// Bindings
// ---------------------------------------------------------------------------

@group(0) @binding(0) var<storage, read_write> vehicles: array<Vehicle>;
@group(0) @binding(1) var<storage, read>       segments: array<RoadSegment>;
@group(0) @binding(2) var<uniform>             params:   IDMParams;
@group(0) @binding(3) var<uniform>             meta:     SimMeta;

// ---------------------------------------------------------------------------
// Helper: extract integer flags from the f32 flags field
// ---------------------------------------------------------------------------
fn getFlags(v: Vehicle) -> u32 {
  return bitcast<u32>(v.flags);
}

fn setFlags(flagsU32: u32) -> f32 {
  return bitcast<f32>(flagsU32);
}

fn hasFlag(v: Vehicle, bit: u32) -> bool {
  return (getFlags(v) & (1u << bit)) != 0u;
}

// ---------------------------------------------------------------------------
// Helper: find the road segment a vehicle is on (nearest segment)
// ---------------------------------------------------------------------------
fn pointToSegmentDistSq(px: f32, py: f32, seg: RoadSegment) -> f32 {
  let dx = seg.endX - seg.startX;
  let dy = seg.endY - seg.startY;
  let lenSq = dx * dx + dy * dy;
  if (lenSq < 0.0001) {
    let ex = px - seg.startX;
    let ey = py - seg.startY;
    return ex * ex + ey * ey;
  }
  var t = ((px - seg.startX) * dx + (py - seg.startY) * dy) / lenSq;
  t = clamp(t, 0.0, 1.0);
  let projX = seg.startX + t * dx;
  let projY = seg.startY + t * dy;
  let ex = px - projX;
  let ey = py - projY;
  return ex * ex + ey * ey;
}

fn findSegment(px: f32, py: f32) -> u32 {
  var bestIdx: u32 = 0u;
  var bestDist: f32 = 1.0e20;
  let count = meta.segmentCount;
  for (var i: u32 = 0u; i < count; i = i + 1u) {
    let d = pointToSegmentDistSq(px, py, segments[i]);
    if (d < bestDist) {
      bestDist = d;
      bestIdx = i;
    }
  }
  return bestIdx;
}

// ---------------------------------------------------------------------------
// Helper: progress along a segment (0..1)
// ---------------------------------------------------------------------------
fn progressOnSegment(px: f32, py: f32, seg: RoadSegment) -> f32 {
  let dx = seg.endX - seg.startX;
  let dy = seg.endY - seg.startY;
  let lenSq = dx * dx + dy * dy;
  if (lenSq < 0.0001) {
    return 0.0;
  }
  var t = ((px - seg.startX) * dx + (py - seg.startY) * dy) / lenSq;
  return clamp(t, 0.0, 1.0);
}

// ---------------------------------------------------------------------------
// Helper: vehicle speed magnitude
// ---------------------------------------------------------------------------
fn speed(v: Vehicle) -> f32 {
  return sqrt(v.velX * v.velX + v.velY * v.velY);
}

// ---------------------------------------------------------------------------
// Helper: lane index from laneOffset and segment lane count
// ---------------------------------------------------------------------------
fn laneIndex(offset: f32, laneCount: f32) -> i32 {
  // Lanes are 3.5m wide, centred at 0.  Lane 0 = leftmost.
  let laneWidth: f32 = 3.5;
  let halfWidth = laneCount * laneWidth * 0.5;
  let idx = i32(floor((offset + halfWidth) / laneWidth));
  return clamp(idx, 0, i32(laneCount) - 1);
}

// ---------------------------------------------------------------------------
// Find nearest leading vehicle on the same segment and lane
// Returns (gap_distance, leader_speed). If no leader found, gap = 1e6.
// ---------------------------------------------------------------------------
fn findLeader(gid: u32, segIdx: u32, myProgress: f32, myLane: i32) -> vec2<f32> {
  var bestGap: f32 = 1.0e6;
  var leaderSpeed: f32 = 0.0;
  let seg = segments[segIdx];
  let segLen = seg.length;
  let count = meta.vehicleCount;

  for (var i: u32 = 0u; i < count; i = i + 1u) {
    if (i == gid) {
      continue;
    }
    let other = vehicles[i];
    // Quick rejection: must be on same segment
    let otherSeg = findSegment(other.posX, other.posY);
    if (otherSeg != segIdx) {
      continue;
    }
    // Check same lane
    let otherLane = laneIndex(other.laneOffset, seg.lanes);
    if (otherLane != myLane) {
      continue;
    }
    // Must be ahead (higher progress)
    let otherProgress = progressOnSegment(other.posX, other.posY, seg);
    if (otherProgress <= myProgress) {
      continue;
    }
    let gap = (otherProgress - myProgress) * segLen - params.vehicleLength;
    if (gap < bestGap) {
      bestGap = gap;
      leaderSpeed = speed(other);
    }
  }
  return vec2<f32>(bestGap, leaderSpeed);
}

// ---------------------------------------------------------------------------
// IDM acceleration
// ---------------------------------------------------------------------------
fn idmAcceleration(v: f32, v0: f32, gap: f32, leaderSpeed: f32) -> f32 {
  // Free-road term
  let vRatio = v / max(v0, 0.001);
  let aFree = params.maxAccel * (1.0 - pow(vRatio, params.delta));

  // Interaction term
  let deltaV = v - leaderSpeed;
  let sqrtAB = sqrt(params.maxAccel * params.comfortDecel);
  let sStar = params.minGap + max(0.0, v * params.timeHeadway + v * deltaV / (2.0 * sqrtAB));
  let gapClamped = max(gap, 0.001);
  let aInteract = -params.maxAccel * (sStar / gapClamped) * (sStar / gapClamped);

  return aFree + aInteract;
}

// ---------------------------------------------------------------------------
// Signal constraint: if red signal within 50m ahead, decelerate to stop
// ---------------------------------------------------------------------------
fn signalDecel(progress: f32, seg: RoadSegment, v: f32) -> f32 {
  // Signal is at the end of the segment
  let distToEnd = (1.0 - progress) * seg.length;
  if (seg.signalState < 0.5 && distToEnd < 50.0 && distToEnd > 0.5) {
    // Smooth deceleration to stop at signal
    let decel = -(v * v) / (2.0 * max(distToEnd, 0.1));
    return max(decel, -params.comfortDecel * 2.0);
  }
  // Yellow: gentle decel if close
  if (seg.signalState > 1.5 && seg.signalState < 2.5 && distToEnd < 30.0 && distToEnd > 0.5) {
    let decel = -(v * v) / (2.0 * max(distToEnd, 0.1));
    return max(decel, -params.comfortDecel);
  }
  return 0.0;
}

// ===========================================================================
// MAIN PHYSICS KERNEL
// ===========================================================================
@compute @workgroup_size(256)
fn physics_main(@builtin(global_invocation_id) gid: vec3<u32>) {
  let idx = gid.x;
  if (idx >= meta.vehicleCount) {
    return;
  }

  var veh = vehicles[idx];
  let flagsU32 = getFlags(veh);

  // If stopped flag is set and no special override, skip
  let isStopped  = (flagsU32 & 4u) != 0u;
  let isConvoy   = (flagsU32 & 1u) != 0u;
  let isDiverted = (flagsU32 & 2u) != 0u;
  let isEmergency = (flagsU32 & 8u) != 0u;

  // Find which road segment this vehicle is on
  let segIdx = findSegment(veh.posX, veh.posY);
  let seg = segments[segIdx];

  // Determine heading direction along segment
  let segDx = seg.endX - seg.startX;
  let segDy = seg.endY - seg.startY;
  let segLen = max(sqrt(segDx * segDx + segDy * segDy), 0.001);
  let dirX = segDx / segLen;
  let dirY = segDy / segLen;

  // Current scalar speed projected along the road direction
  let v = max(veh.velX * dirX + veh.velY * dirY, 0.0);

  // Effective desired speed: scale by congestion and speed limit
  let v0 = min(params.desiredSpeed, seg.speedLimitMs) * (1.0 - seg.congestionIdx * 0.5);

  // Find leading vehicle
  let myProgress = progressOnSegment(veh.posX, veh.posY, seg);
  let myLane = laneIndex(veh.laneOffset, seg.lanes);
  let leaderInfo = findLeader(idx, segIdx, myProgress, myLane);
  let gap = leaderInfo.x;
  let leaderSpeed = leaderInfo.y;

  // Compute IDM acceleration
  var accel = idmAcceleration(v, v0, gap, leaderSpeed);

  // Check if vehicle's lane is closed
  let closedMask = u32(seg.closedLanes);
  let myLaneU = u32(myLane);
  if ((closedMask & (1u << myLaneU)) != 0u) {
    // Lane is closed, decelerate to stop
    accel = min(accel, -params.comfortDecel);
  }

  // Apply signal constraints — convoy and emergency vehicles bypass
  if (!isConvoy && !isEmergency) {
    let sigDecel = signalDecel(myProgress, seg, v);
    if (sigDecel < 0.0) {
      accel = min(accel, sigDecel);
    }
  }

  // Clamp acceleration
  accel = clamp(accel, -params.comfortDecel * 2.0, params.maxAccel);

  // Compute new speed (scalar along road direction)
  var newV = v + accel * params.dt;
  newV = max(newV, 0.0);  // no reversing

  // Euler integration
  veh.velX = newV * dirX;
  veh.velY = newV * dirY;
  veh.posX = veh.posX + veh.velX * params.dt;
  veh.posY = veh.posY + veh.velY * params.dt;
  veh.accelX = accel * dirX;
  veh.accelY = accel * dirY;

  // Update stopped flag
  var newFlags = flagsU32 & ~4u;  // clear stopped bit
  if (newV < 0.1) {
    newFlags = newFlags | 4u;     // set stopped bit
  }
  veh.flags = setFlags(newFlags);

  // Write back
  vehicles[idx] = veh;
}

// ===========================================================================
// MOBIL LANE-CHANGE KERNEL (simplified)
// ===========================================================================

// MOBIL politeness factor:
//   Regular traffic: p = 0.5
//   Convoy vehicles: p = 0.0 (aggressive)
// A lane change is executed if:
//   accel_new - accel_old > p * (decel_new_follower - decel_old_follower) + a_threshold
// For simplicity we approximate by checking gap acceptability in adjacent lanes.

@compute @workgroup_size(256)
fn lane_change_main(@builtin(global_invocation_id) gid: vec3<u32>) {
  let idx = gid.x;
  if (idx >= meta.vehicleCount) {
    return;
  }

  var veh = vehicles[idx];
  let flagsU32 = getFlags(veh);
  let isConvoy = (flagsU32 & 1u) != 0u;
  let isStopped = (flagsU32 & 4u) != 0u;

  // Don't lane-change if stopped
  if (isStopped) {
    return;
  }

  let segIdx = findSegment(veh.posX, veh.posY);
  let seg = segments[segIdx];
  let laneCount = i32(seg.lanes);

  if (laneCount <= 1) {
    return; // single lane, no change possible
  }

  let myLane = laneIndex(veh.laneOffset, seg.lanes);
  let myProgress = progressOnSegment(veh.posX, veh.posY, seg);
  let v = speed(veh);

  // Politeness factor
  let politeness = select(0.5, 0.0, isConvoy);

  // Acceleration threshold for lane change incentive
  let aThreshold: f32 = 0.2;

  // Current lane IDM accel
  let currentLeader = findLeader(idx, segIdx, myProgress, myLane);
  let currentAccel = idmAcceleration(v, min(params.desiredSpeed, seg.speedLimitMs), currentLeader.x, currentLeader.y);

  // Check both adjacent lanes
  var bestLane = myLane;
  var bestGain: f32 = 0.0;

  for (var d: i32 = -1; d <= 1; d = d + 2) {
    let targetLane = myLane + d;
    if (targetLane < 0 || targetLane >= laneCount) {
      continue;
    }

    // Check if target lane is closed
    let closedMask = u32(seg.closedLanes);
    if ((closedMask & (1u << u32(targetLane))) != 0u) {
      continue;
    }

    // Find leader in target lane
    let targetLeader = findLeader(idx, segIdx, myProgress, targetLane);
    let targetGap = targetLeader.x;

    // Safety: require minimum gap in target lane
    let safeGap = params.minGap + v * 0.5;
    if (targetGap < safeGap) {
      continue;
    }

    // Check follower gap in target lane (simplified: scan for nearest behind)
    var followerGap: f32 = 1.0e6;
    let count = meta.vehicleCount;
    for (var i: u32 = 0u; i < count; i = i + 1u) {
      if (i == idx) { continue; }
      let other = vehicles[i];
      let otherSeg = findSegment(other.posX, other.posY);
      if (otherSeg != segIdx) { continue; }
      let otherLane = laneIndex(other.laneOffset, seg.lanes);
      if (otherLane != targetLane) { continue; }
      let otherProgress = progressOnSegment(other.posX, other.posY, seg);
      if (otherProgress >= myProgress) { continue; } // ahead, not follower
      let g = (myProgress - otherProgress) * seg.length - params.vehicleLength;
      if (g < followerGap) {
        followerGap = g;
      }
    }

    if (followerGap < safeGap) {
      continue;
    }

    // Incentive: acceleration gain in target lane
    let targetAccel = idmAcceleration(v, min(params.desiredSpeed, seg.speedLimitMs), targetGap, targetLeader.y);
    let gain = targetAccel - currentAccel - politeness * params.comfortDecel - aThreshold;

    if (gain > bestGain) {
      bestGain = gain;
      bestLane = targetLane;
    }
  }

  // Execute lane change if beneficial
  if (bestLane != myLane) {
    let laneWidth: f32 = 3.5;
    let halfWidth = seg.lanes * laneWidth * 0.5;
    let newOffset = f32(bestLane) * laneWidth + laneWidth * 0.5 - halfWidth;
    veh.laneOffset = newOffset;
    vehicles[idx] = veh;
  }
}
