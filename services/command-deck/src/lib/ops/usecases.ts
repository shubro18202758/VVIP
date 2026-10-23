/**
 * The eight corridor-event use-case profiles.
 *
 * Every field is transcribed from the use-case configs shipped with the
 * showcase ask (scenario_id, route spine, seeds, baseline closure policy,
 * departure, replications, horizon, description). The route spines mirror
 * the emulator's use_case_library.json and the twin's scenarios.py on the
 * real (OpenStreetMap) network — each a legal path, one-ways driven their
 * own way (the contract tests hold them equal). They serve three purposes:
 *
 *   1. OFFLINE FIXTURES for the multi-event board when GET /api/v1/events/active
 *      is not deployed yet (rows are tagged `offline-fixture` and labelled).
 *   2. Scenario picker + explicit fields for the twin shadow-eval panel.
 *   3. Launch presets surfaced to the emulator panel (use_case_id values).
 *
 * event_kind and crowd_class are the showcase's mapping of each profile onto
 * the generalized axes; the mapping follows the profile descriptions.
 */

import type {
	ClosurePolicySpec,
	CrowdClass,
	EventKind,
	EventPhase,
	ProtectionClass,
} from './types';

export interface UseCaseProfile {
	id: string;
	scenarioId: string;
	name: string;
	eventKind: EventKind;
	protectionClass: ProtectionClass;
	crowdClass: CrowdClass;
	routeSegmentIds: number[];
	departureTime: string; // ISO 8601 with +05:30
	baselinePolicy: ClosurePolicySpec;
	seed: number;
	nReplications: number;
	horizonMin: number;
	phases: EventPhase[];
	description: string;
}

const ISO_IST = (d: string) => d; // configs already carry +05:30 offsets

function movementPhases(
	departure: string,
	horizonMin: number,
	policy: ClosurePolicySpec,
	extra?: EventPhase[],
): EventPhase[] {
	const start = new Date(departure).toISOString();
	const end = new Date(new Date(departure).getTime() + horizonMin * 60_000).toISOString();
	const main: EventPhase = {
		label: 'movement window',
		startTime: start,
		endTime: end,
		closurePolicy: {
			advanceClosureSec: policy.advanceClosureSec,
			closureType: policy.closureType,
		},
		state: 'planned',
	};
	return extra ? [...extra, main] : [main];
}

export const USE_CASES: UseCaseProfile[] = [
	{
		id: 'vvip_convoy_base',
		scenarioId: 'vvip_convoy_base',
		name: 'VVIP Convoy — Base Case (Z+ state-capital run)',
		eventKind: 'protected_movement',
		protectionClass: 'Z+',
		crowdClass: 'LOW',
		routeSegmentIds: [1026, 1025, 1024, 1048, 1028, 1027, 1043],
		departureTime: ISO_IST('2026-11-02T10:10:00+05:30'),
		baselinePolicy: { advanceClosureSec: 180, closureType: 'full_closure' },
		seed: 20261102,
		nReplications: 10,
		horizonMin: 45,
		phases: movementPhases('2026-11-02T10:10:00+05:30', 45, {
			advanceClosureSec: 180,
			closureType: 'full_closure',
		}),
		description:
			'Z+ protected movement from Gandhinagar into the city core on a Monday mid-morning: down the Gandhinagar-Ahmedabad Highway (1026-1025-1024) through Koba and Bhat Circles, over the Indira Bridge (1048), along Airport Road (1028-1027) to Shahibaug and in on Dr. Ambedkar Road (1043) to Lal Darwaja. Maximum-protocol closure (180 s advance, full closure), default event rates, no weather.',
	},
	{
		id: 'rath_yatra_ahmedabad',
		scenarioId: 'rath_yatra_ahmedabad',
		name: 'Jagannath Rath Yatra — Old-City Procession',
		eventKind: 'procession',
		protectionClass: 'X',
		crowdClass: 'EXTREME',
		routeSegmentIds: [1012, 1049, 1013],
		departureTime: ISO_IST('2025-06-27T07:00:00+05:30'),
		baselinePolicy: { advanceClosureSec: 120, closureType: 'full_closure' },
		seed: 20250627,
		nReplications: 12,
		horizonMin: 60,
		phases: movementPhases('2025-06-27T07:00:00+05:30', 60, {
			advanceClosureSec: 120,
			closureType: 'full_closure',
		}),
		description:
			'148th Ahmedabad Rath Yatra: three chariots step off at 07:00 from the Jamalpur temple and crawl the old-city bazaar belt — east along one-way Gandhi Road to Kalupur (1012), south past Sarangpur Darwaja (1049) and west along one-way Swami Vivekananda Road (1013), Relief Road (1005) shut alongside (~14 km). Managed as an X-class movement at chariot pace (8 km/h) with full street closure rolling ahead and lakhs-strong crowd load.',
	},
	{
		id: 'kumbh_scale_gathering',
		scenarioId: 'kumbh_scale_gathering',
		name: 'Kumbh-Class Mass Gathering — Perimeter Zone',
		eventKind: 'mass_gathering',
		protectionClass: 'X',
		crowdClass: 'EXTREME',
		routeSegmentIds: [1001, 1006, 1009],
		departureTime: ISO_IST('2025-01-29T04:30:00+05:30'),
		baselinePolicy: { advanceClosureSec: 90, closureType: 'signal_hold' },
		seed: 20250129,
		nReplications: 12,
		horizonMin: 60,
		phases: [
			{
				label: 'mela perimeter (multi-week schedule, 6–12 h daily windows)',
				startTime: '2025-01-28T18:30:00.000Z',
				endTime: null,
				closurePolicy: null,
				state: 'planned',
			},
			...movementPhases('2025-01-29T04:30:00+05:30', 60, {
				advanceClosureSec: 90,
				closureType: 'signal_hold',
			}),
		],
		description:
			'A Kumbh-class bathing-day window: the zone interior is car-free and frozen; the engine manages the perimeter ring and ingress surges. X-class supply/medical shuttle enters at 04:30 pre-dawn under signal_hold (staged ingress, not blanket freeze).',
	},
	{
		id: 'riverfront_marathon',
		scenarioId: 'riverfront_marathon',
		name: 'City Marathon — Sabarmati Riverfront Race Corridor',
		eventKind: 'race',
		protectionClass: 'X',
		crowdClass: 'HIGH',
		routeSegmentIds: [1007, 1015, 1001, 1006],
		departureTime: ISO_IST('2024-11-24T05:45:00+05:30'),
		baselinePolicy: { advanceClosureSec: 60, closureType: 'full_closure' },
		seed: 20241124,
		nReplications: 10,
		horizonMin: 60,
		phases: movementPhases('2024-11-24T05:45:00+05:30', 60, {
			advanceClosureSec: 60,
			closureType: 'full_closure',
		}),
		description:
			'Adani Ahmedabad Marathon class (24,000+ runners): 05:45 gun start, race corridor closed to vehicles in the dark and reopened in stages behind the sweep. X-class pace/sweep convoy at 12 km/h under full closure with spectator spillover on the approaches.',
	},
	{
		id: 'national_day_parade',
		scenarioId: 'national_day_parade',
		name: 'Republic Day / Independence Day — Ceremonial Parade Corridor',
		eventKind: 'ceremonial',
		protectionClass: 'Z',
		crowdClass: 'HIGH',
		routeSegmentIds: [1029, 1028, 1048, 1024, 1025],
		departureTime: ISO_IST('2027-01-26T08:35:00+05:30'),
		baselinePolicy: { advanceClosureSec: 150, closureType: 'full_closure' },
		seed: 20270126,
		nReplications: 10,
		horizonMin: 60,
		phases: movementPhases('2027-01-26T08:35:00+05:30', 60, {
			advanceClosureSec: 150,
			closureType: 'full_closure',
		}),
		description:
			'The two fixed ceremonial days every year: dignitary runs from SVPI airport (1029-1028) over the Indira Bridge (1048) onto the GN Highway parade strip at Bhat (1024-1025) at 08:35, arriving before the 09:00 flag, while spectator and parking load saturates the strip.',
	},
	{
		id: 'motera_stadium_egress',
		scenarioId: 'motera_stadium_egress',
		name: 'Stadium Egress — Narendra Modi Stadium Match Night',
		eventKind: 'egress_surge',
		protectionClass: 'X',
		crowdClass: 'EXTREME',
		routeSegmentIds: [1032, 1041, 1009, 1002, 1001],
		departureTime: ISO_IST('2026-03-08T20:05:00+05:30'),
		baselinePolicy: { advanceClosureSec: 60, closureType: 'signal_hold' },
		seed: 20260308,
		nReplications: 12,
		horizonMin: 60,
		phases: movementPhases('2026-03-08T20:05:00+05:30', 60, {
			advanceClosureSec: 60,
			closureType: 'signal_hold',
		}),
		description:
			'The 90–120 min egress surge just after a final ball at the world’s largest cricket ground: an X-class ambulance with a green-corridor request fights the ~130k dispersal wave from Motera Stadium Road (1032) over the Chimanbhai Patel Flyover (1041) down the Ashram spine (1009-1002-1001) under signal_hold.',
	},
	{
		id: 'flood_evacuation_corridor',
		scenarioId: 'flood_evacuation_corridor',
		name: 'Monsoon Flood — Evacuation and Relief Corridor',
		eventKind: 'emergency_evacuation',
		protectionClass: 'X',
		crowdClass: 'HIGH',
		routeSegmentIds: [1040, 1021, 1022, 1008],
		departureTime: ISO_IST('2024-08-27T11:20:00+05:30'),
		baselinePolicy: { advanceClosureSec: 60, closureType: 'signal_hold' },
		seed: 20240827,
		nReplications: 10,
		horizonMin: 60,
		phases: movementPhases('2024-08-27T11:20:00+05:30', 60, {
			advanceClosureSec: 60,
			closureType: 'signal_hold',
		}),
		description:
			'Gujarat floods class (~280 mm/24 h): the low-lying river spine waterlogs while an X-class relief column runs the reverse-flow evacuation spine — Sarkhej (1040) onto SP Ring Road (1021-1022-1008) — in 22 mm/h rain under signal_hold, with evacuee traffic surging onto the ring road behind it.',
	},
	{
		id: 'metro_work_zone',
		scenarioId: 'metro_work_zone',
		name: 'Planned Infrastructure Work Zone — Metro/Bullet-Train Works',
		eventKind: 'work_zone',
		protectionClass: 'X',
		crowdClass: 'MODERATE',
		routeSegmentIds: [1031, 1014, 1049, 1038],
		departureTime: ISO_IST('2025-08-14T09:15:00+05:30'),
		baselinePolicy: { advanceClosureSec: 60, closureType: 'partial_closure' },
		seed: 20250814,
		nReplications: 8,
		horizonMin: 60,
		phases: [
			{
				label: 'published lane loss — Kankaria Road + Kalupur Bridge (3 Jul – 30 Sep)',
				startTime: '2025-07-02T18:30:00.000Z',
				endTime: '2025-09-29T18:30:00.000Z',
				closurePolicy: { advanceClosureSec: null, closureType: 'partial_closure' },
				state: 'planned',
			},
			...movementPhases('2025-08-14T09:15:00+05:30', 60, {
				advanceClosureSec: 60,
				closureType: 'partial_closure',
			}),
		],
		description:
			'The most predictable corridor event: months-long lane loss published in advance. An X-class inspection convoy runs Kasturba Gandhi Road (1031) through the Kalupur pinch (1014), south past Sarangpur Darwaja (1049) and onto Kankaria Road (1038) while both work zones hold two barricaded lanes and diverted bazaar traffic squeezes Kankaria Road itself.',
	},
];

export function useCaseById(id: string): UseCaseProfile | undefined {
	return USE_CASES.find((u) => u.id === id || u.scenarioId === id);
}
