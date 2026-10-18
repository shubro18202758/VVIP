/**
 * The registered God's-eye layers (chip order = array order).
 *
 * Registry mechanics ported from gods-eye-view (MIT) — see registry.ts.
 * Each layer is a self-contained module following the gev policy/model/
 * records/source/presentation split flattened to one file per Veer layer.
 */

import { validateLayers, type GodsEyeLayer } from '../registry';
import { agencyUnitsLayer } from './agency-units';
import { camerasLayer } from './cameras';
import { corridorEventsLayer } from './corridor-events';
import { signalNetworkLayer } from './signal-network';
import { trafficFlowLayer } from './traffic-flow';
import { vehicleTracesLayer } from './vehicle-traces';

export const GODS_EYE_LAYERS: ReadonlyArray<GodsEyeLayer> = validateLayers([
	corridorEventsLayer,
	trafficFlowLayer,
	vehicleTracesLayer,
	signalNetworkLayer,
	agencyUnitsLayer,
	camerasLayer,
]);

export const GODS_EYE_LAYER_IDS: ReadonlyArray<string> = GODS_EYE_LAYERS.map((l) => l.id);
