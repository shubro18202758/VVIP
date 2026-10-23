/**
 * The registered God's-eye layers (chip order = array order).
 *
 * Registry mechanics ported from gods-eye-view (MIT) — see registry.ts.
 * Each layer is a self-contained module following the gev policy/model/
 * records/source/presentation split flattened to one file per Veer layer.
 * Every pick prefix is distinct (validateLayers enforces no overlap) and
 * every layer maps its picks to C-C URNs (urnOf), so a click anywhere
 * selects the same entity the fusion rail and the dossier address.
 * photoreal-3d (C-L) is the one keyed layer (requiresKeyId 'ion'): Google
 * Photorealistic 3D Tiles on the env-keyed deck, 'needs
 * VEER_CESIUM_ION_TOKEN' otherwise; it draws scenery and claims no pick.
 */

import { validateLayers, type GodsEyeLayer } from '../registry';
import { agencyUnitsLayer } from './agency-units';
import { anomaliesLayer } from './anomalies';
import { camerasLayer } from './cameras';
import { convoyLayer } from './convoy';
import { corridorEventsLayer } from './corridor-events';
import { dispatchLayer } from './dispatch';
import { enforcementLayer } from './enforcement';
import { photoreal3dLayer } from './photoreal-3d';
import { signalNetworkLayer } from './signal-network';
import { trafficFlowLayer } from './traffic-flow';
import { vehicleTracesLayer } from './vehicle-traces';

export const GODS_EYE_LAYERS: ReadonlyArray<GodsEyeLayer> = validateLayers([
	corridorEventsLayer,
	trafficFlowLayer,
	vehicleTracesLayer,
	enforcementLayer,
	signalNetworkLayer,
	agencyUnitsLayer,
	dispatchLayer,
	convoyLayer,
	camerasLayer,
	anomaliesLayer,
	photoreal3dLayer,
]);

export const GODS_EYE_LAYER_IDS: ReadonlyArray<string> = GODS_EYE_LAYERS.map((l) => l.id);
