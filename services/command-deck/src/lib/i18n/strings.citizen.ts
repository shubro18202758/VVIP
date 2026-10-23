/**
 * Citizen-screens string pack (EN / HI / GU) — the public copy for the
 * rebuilt /citizen advisory, the /driver green-corridor flow, the (deck)
 * /comms advisory desk and the /chat console.
 *
 * The i18n CORE (strings.ts) is frozen per contract; this module registers
 * its keys additively via the design-shell `registerPack()` extension
 * point (idempotent, never overrides). `citizenT()` additionally resolves
 * against this pack FIRST and falls back to the core `t()` — the same
 * pattern as the ops stream's `tOps()` — so components have one resolver
 * and the frozen packs stay intact even if registration has not run.
 *
 * Copy rules (per the language spec): plain-language consequences, never
 * internal codes — citizens never see GREEN/AMBER/RED/BLACK; road names stay
 * transliterated as on city signage; every mutable number is localized by the
 * caller through localizeNumber().
 */

import { localizeNumber, registerPack, t } from './strings';

type Pack = Record<string, string | ((...args: unknown[]) => string)>;

// ─── EN ─────────────────────────────────────────────────────────────────────

const en: Pack = {
	// ── /citizen — "Your road, next 2 hours" ────────────────────────────
	citizen_kicker: 'Your road, next 2 hours',
	citizen_explainer_link: 'Why roads hold',
	strip_calm: 'No corridor holds announced right now.',
	strip_watch: 'An event route is active — expect short planned holds near it.',
	strip_hold: 'Planned holds are running on the routes below. They lift on schedule.',
	strip_severe: 'Priority corridor traffic is running. Civilian holds stay short and are lifted junction by junction.',
	card_hold_label: 'Roads hold',
	card_window_at: (clock) => `around ${clock}`,
	card_now: 'Holding now',
	card_soon: 'Next',
	card_alt_label: 'Use instead',
	timeline_title: 'Today along your route',
	timeline_empty: 'No more corridor events scheduled today.',
	timeline_lifted: 'lifted',
	timeline_planned: 'planned',
	subscribe_demo_note: 'Demo prototype — no messages will ever be sent from this build.',
	footer_privacy: 'No location is collected. Advisories name stretches and times only.',

	// Plain-language consequences per event kind (why the road holds).
	consequence_protected_movement:
		'A protected movement is passing — holds lift as it clears each junction.',
	consequence_procession: 'A procession is walking the route — crossings close briefly while it passes.',
	consequence_mass_gathering: 'A large gathering is dispersing — traffic is metered for crowd safety.',
	consequence_race: 'A road race is on — the stretch stays closed until the last runner clears.',
	consequence_ceremonial: 'A ceremonial parade is passing — short rolling holds only.',
	consequence_egress_surge: 'Stadium crowds are leaving — exits are metered in waves.',
	consequence_emergency_evacuation:
		'Relief and emergency traffic has priority — civilian traffic is held briefly.',
	consequence_work_zone: 'Road work narrows the carriageway — traffic passes one lane at a time.',
	// Operator-only kinds never reach a public card (holds.ts allowlist);
	// the copy exists so no surface can ever render a raw key.
	consequence_vehicle_pursuit: 'A police operation is active here — follow officers’ directions.',
	consequence_crime_incident: 'An incident response is active here — follow officers’ directions.',
	consequence_unknown: 'A planned corridor operation is active — expect short holds.',
	card_hold_unknown: 'Hold length not published yet.',
	card_hold_from_policy: 'hold length from the event’s closure plan',
	card_hold_from_adaptive: 'hold length adapted to live traffic',
	card_hold_from_baseline: 'standard hold length — not yet adapted to live traffic',
	card_hold_unavailable: 'Hold length unavailable right now — check again in a minute.',
	card_alt_none: 'No signed alternate on the modelled roads — allow extra time.',
	card_alt_unavailable: 'Alternate route unavailable right now — check again in a minute.',
	card_alt_from_graph: 'alternate computed on the city road graph',
	citizen_feed_offline: 'Live advisories are unreachable — showing the labelled offline preview.',
	citizen_feed_connecting: 'Connecting to the traffic control room…',
	citizen_feed_stale: 'Live advisories are briefly unreachable — showing the last update.',
	citizen_none_now: 'No corridor events in the next 2 hours.',
	citizen_preview_none: 'No public corridor event to preview right now.',
	map_unavailable: 'Map unavailable',
	map_unavailable_hint: 'The map could not start on this device.',

	// ── /driver — "The Window" ──────────────────────────────────────────
	driver_kicker: 'Green corridor',
	golden_hour: 'Golden hour',
	golden_elapsed: 'elapsed',
	phase_idle: 'idle',
	phase_evaluating: 'evaluating',
	phase_proposal: 'proposal',
	phase_open: 'window open',
	phase_deferred: 'deferred',
	doctrine_note: 'Green corridors open 60–180 seconds before the ambulance reaches each junction.',
	scripted_badge: 'SCRIPTED FALLBACK',
	scripted_note:
		'No live corridor event right now — this is the labelled scripted fallback: nothing is sent and every number is demo-scale.',
	corridor_map: 'Corridor map',
	corridor_granted_stretch: 'Granted stretch',
	unit_en_route: 'Jan Rakshak unit en route',
	unit_on_scene: 'Jan Rakshak unit on scene',
	units_note_fallback: 'Scripted fallback — this unit walk is simulated, not a live dispatch.',
	control_room: 'Control room',
	control_room_line: '108 control · stay on channel',
	after_action: (n) => `~${n} minutes saved vs documented 5–20 minute holds`,
	map_note_fallback: 'Road geometry is unreachable — corridor lines are not drawn.',

	// ── /driver — the live request → human approval → window flow ───────
	driver_unit_label: 'Ambulance unit',
	driver_unit_hint: 'Your 108 unit number',
	driver_unit_invalid: 'Use your unit number, e.g. 108-14.',
	driver_stretch_option: (road, seg) => `${road} · stretch ${seg}`,
	driver_event_label: (kind) => `through: ${kind}`,
	phase_requested: 'awaiting duty officer',
	phase_approved: 'approved',
	driver_awaiting_title: 'Awaiting duty officer',
	driver_awaiting_body:
		'Your request is on the control-room wall. The window opens only when a named duty officer approves it.',
	driver_request_ref: (id) => `request ${id}`,
	driver_deferred_body: 'The control room deferred this request — hold position and request again if needed.',
	driver_cancelled_body: 'The control room cancelled this request.',
	driver_primed_title: 'Approved — the corridor is being primed',
	driver_primed_countdown: (sec) => `Window opens in ${sec} s`,
	driver_primed_when: (clock) => `at ${clock} — timed to your arrival at the junction`,
	driver_window_from_policy: (sec) => `window ${sec} s — from the event’s closure plan`,
	driver_window_from_adaptive: (sec) => `window ${sec} s — adapted to live traffic`,
	driver_window_from_baseline: (sec) => `window ${sec} s — standard length, not yet adapted to live traffic`,
	driver_window_clamped: (raw) => `(event value ${raw} s, held to the 60–180 s doctrine)`,
	driver_window_anchor_eta: 'opens before the unit reaches the junction, closes as it arrives',
	driver_window_anchor_approval: 'runs from the officer’s approval — the unit’s arrival is not projected',
	driver_window_unpublished: 'Window length not published for this event — follow 108 control.',
	driver_unit_eta: (sec) => `unit ~${sec} s from the junction`,
	driver_unit_arrived: 'unit at the junction',
	driver_unit_no_position: 'unit position not projected yet',
	driver_unit_emulated: 'EMULATED unit',
	driver_window_closed: 'Window closed — the corridor returns to its planned holds.',
	driver_request_failed: (msg) => `Request not sent — ${msg}`,
	driver_status_retry: 'Status feed unreachable — retrying…',
	driver_live_note:
		'Live: the request, the officer’s decision and every status change are recorded in the dispatch audit trail.',
	driver_scripted_approval: 'Scripted approval — no duty officer is in the loop (fallback only).',

	// Dispatch lifecycle words (units/router.py DISPATCH_TRANSITIONS).
	dispatch_status_requested: 'requested',
	dispatch_status_approved: 'approved',
	dispatch_status_en_route: 'en route',
	dispatch_status_on_scene: 'on scene',
	dispatch_status_completed: 'completed',
	dispatch_status_deferred: 'deferred',
	dispatch_status_cancelled: 'cancelled',

	// ── /comms — advisory desk (operator) ───────────────────────────────
	comms_title: 'Advisory desk',
	comms_sub: 'One fact, every audience, every language — drafted for an officer to send, never auto-sent.',
	comms_pick_event: 'Event',
	comms_generate: 'Generate advisory',
	comms_regenerate: 'Regenerate',
	comms_copy: 'Copy',
	comms_copied: 'Copied',
	comms_audience: 'Audience',
	audience_citizens: 'Citizens · SMS / WhatsApp',
	audience_zones: 'Zone officers',
	audience_108: '108 control',
	audience_municipal: 'Municipal',
	comms_preview_note: 'Preview — the exact text each audience would receive.',
	comms_template_title: 'Templates by event kind',
	comms_draft_log: 'Send / draft log',
	comms_drafted_badge: 'DRAFTED — NOT DELIVERED',
	comms_no_gateway:
		'No message gateway is deployed. Drafts are prepared here for an officer to copy and send — nothing is delivered or written to the audit record.',
	comms_dispatch_threads: 'Dispatch threads',
	comms_dispatch_live: 'live',
	comms_dispatch_demo: 'scripted demo',
	comms_dispatch_none: 'No dispatches recorded for this event yet.',
	comms_all_clear: 'Draft all-clear',
	comms_session_only: 'Drafts stay in this browser tab — nothing is sent or written to the audit record.',
	comms_agencies: 'Agencies',
	comms_not_deployed: 'not deployed',
	comms_scope_event: 'This event',
	comms_scope_all: 'All events',
	comms_other_events: (n) => `${n} more on other events`,
	comms_withheld: 'Operator-only event kind — no citizen advisory is drafted for it.',
	comms_template_hint: 'Pick a kind to preview its citizen template.',

	// Generated advisory templates (args are already-localized strings).
	adv_citizen_hold: (kind, minutes, clock, alt) =>
		`${kind}: ~${minutes} min hold on the route from ${clock}. Use ${alt} instead. Roads reopen on time.`,
	adv_citizen_hold_noalt: (kind, minutes, clock) =>
		`${kind}: ~${minutes} min hold on the route from ${clock}. No signed alternate — allow extra time. Roads reopen on time.`,
	adv_citizen_hold_altunknown: (kind, minutes, clock) =>
		`${kind}: ~${minutes} min hold on the route from ${clock}. Allow extra time. Roads reopen on time.`,
	adv_citizen_clear: (kind) =>
		`${kind} has cleared. Roads are open on time. Thank you for your patience.`,
	adv_zone: (kind, seg, minutes) =>
		`Zone officers: ${kind} hold on stretch ${seg}, ~${minutes} min. Hold the line, then release on schedule.`,
	adv_108: (kind, seg) =>
		`108 control: ${kind} active on stretch ${seg}. Green-corridor requests are being answered for this stretch.`,
	adv_municipal: (kind, seg, minutes) =>
		`Municipal: ${kind} on stretch ${seg}, planned ~${minutes} min holds. Barricade teams release junction by junction.`,

	// ── /chat — agent console ───────────────────────────────────────────
	chat_title: 'Agent console',
	chat_sub: 'Corridor brain · streamed reasoning over the selected event',
	chat_context: 'Context',
	chat_context_none: 'No event selected — the console runs without event context.',
	chat_placeholder: 'Ask about the corridor…',
	chat_send: 'Send',
	chat_clear: 'Clear',
	chat_offline: 'Agent offline',
	chat_offline_note: 'The agent stream is not answering — the console is read-only until it returns.',
	chat_thoughts: 'Reasoning',
	chat_tools: 'Tool calls',
	chat_you: 'You',
	chat_agent: 'Agent',
	chat_tool_pending: 'pending',
	chat_tool_running: 'running',
	chat_tool_success: 'done',
	chat_tool_error: 'failed',
	chat_tiers: 'tiers',
	chat_tier_decider: 'decider',
	chat_tier_gemma: 'Gemma',
	chat_tier_heuristic: 'heuristic',
	chat_degraded_turn: 'degraded turn',
	chat_raw_envelope: 'No prose answer — the brain returned a raw JSON envelope (verbatim below).',
	chat_alerts: 'operator alerts',
	chat_session: 'session',
	chat_action: 'action',

	// ── wave 3: provenance, the 2 h horizon, event phases ───────────────
	citizen_emulated_badge: 'SIMULATED FEED · EMULATED',
	citizen_emulated_note:
		'Cards marked SIMULATED FEED are an event-emulator demonstration run — not real road holds.',
	citizen_later_title: 'Later — beyond the next 2 hours',
	citizen_later_note: 'Hold times are published closer to the start.',
	citizen_later_unscheduled: 'time not set',
	citizen_later_roads_withheld: 'route announced within 2 hours of the start',
	card_sim_now: 'Simulated · now',
	adv_emulated_prefix: '[SIMULATED FEED · EMULATED — exercise text, not a real road hold]',
	card_sim_soon: 'Simulated · next',
	consequence_planned_protected_movement:
		'A protected movement will pass — holds will lift as it clears each junction.',
	consequence_planned_procession: 'A procession will walk the route — crossings will close briefly while it passes.',
	consequence_planned_mass_gathering: 'A large gathering will disperse — traffic will be metered for crowd safety.',
	consequence_planned_race: 'A road race will run — the stretch will stay closed until the last runner clears.',
	consequence_planned_ceremonial: 'A ceremonial parade will pass — short rolling holds only.',
	consequence_planned_egress_surge: 'Stadium crowds will leave — exits will be metered in waves.',
	consequence_planned_emergency_evacuation:
		'Relief and emergency traffic will have priority — civilian traffic will be held briefly.',
	consequence_planned_work_zone: 'Road work will narrow the carriageway — traffic will pass one lane at a time.',
	consequence_planned_unknown: 'A planned corridor operation will run — expect short holds.',
	phase_closure: 'closure',
	phase_works: 'road works',
	phase_setup: 'set-up',
	phase_procession: 'procession',
	phase_assembly: 'assembly',
	phase_approach: 'approach',
	phase_transit: 'transit',
	phase_egress: 'egress',
	phase_dispersal: 'dispersal',
	phase_evacuation: 'evacuation',
	phase_parade: 'parade',
	phase_race: 'race',
	phase_movement_window: 'movement window',
	phase_generic: (n) => `phase ${n}`,
	driver_new_request: 'New request',
	driver_emulated_hidden: (n) =>
		`${n} simulated (EMULATED) corridor event(s) not offered — a request can only name a real corridor.`,
};

// ─── HI ─────────────────────────────────────────────────────────────────────

const hi: Pack = {
	// ── /citizen ────────────────────────────────────────────────────────
	citizen_kicker: 'आपकी सड़क, अगले २ घंटे',
	citizen_explainer_link: 'सड़कें क्यों रुकती हैं',
	strip_calm: 'अभी कोई कॉरिडोर ठहराव घोषित नहीं है।',
	strip_watch: 'एक घटना मार्ग सक्रिय है — उसके पास छोटे नियोजित ठहराव संभावित हैं।',
	strip_hold: 'नीचे दिए मार्गों पर नियोजित ठहराव चल रहे हैं। समय पर ही खुलेंगे।',
	strip_severe: 'प्राथमिकता वाला कॉरिडोर ट्रैफ़िक चल रहा है। आम ठहराव छोटे रहते हैं और चौराहे-दर-चौराहे खुलते हैं।',
	card_hold_label: 'सड़क रुकेगी',
	card_window_at: (clock) => `लगभग ${clock} बजे`,
	card_now: 'अभी रुका है',
	card_soon: 'अगला',
	card_alt_label: 'इसके बदले चलें',
	timeline_title: 'आज आपके मार्ग पर',
	timeline_empty: 'आज कोई और कॉरिडोर घटना नियोजित नहीं है।',
	timeline_lifted: 'हट गया',
	timeline_planned: 'नियोजित',
	subscribe_demo_note: 'डेमो प्रोटोटाइप — इस बिल्ड से कोई संदेश नहीं भेजा जाएगा।',
	footer_privacy: 'कोई लोकेशन नहीं ली जाती। सलाह में केवल मार्ग और समय होते हैं।',

	consequence_protected_movement:
		'एक संरक्षित आवाजाही गुजर रही है — जैसे ही वह चौराहा पार करती है, ठहराव खुलता जाता है।',
	consequence_procession: 'जुलूस मार्ग पर चल रहा है — गुज़रने तक क्रॉसिंग थोड़ी देर बंद रहती है।',
	consequence_mass_gathering: 'बड़ी भीड़ बिखर रही है — सुरक्षा के लिए ट्रैफ़िक को थोड़ा-थोड़ा छोड़ा जा रहा है।',
	consequence_race: 'सड़क दौड़ चल रही है — अंतिम धावक निकलने तक मार्ग बंद रहेगा।',
	consequence_ceremonial: 'एक परेड गुजर रही है — केवल छोटे घुमावदार ठहराव।',
	consequence_egress_surge: 'स्टेडियम से भीड़ लौट रही है — निकासी लहरों में हो रही है।',
	consequence_emergency_evacuation:
		'राहत और आपातकालीन ट्रैफ़िक को प्राथमिकता है — आम ट्रैफ़िक थोड़ी देर रुकता है।',
	consequence_work_zone: 'सड़क कार्य से रास्ता सँकड़ा है — एक समय पर एक लेन।',
	consequence_vehicle_pursuit: 'यहाँ पुलिस कार्रवाई चल रही है — अधिकारियों के निर्देश मानें।',
	consequence_crime_incident: 'यहाँ घटना पर कार्रवाई चल रही है — अधिकारियों के निर्देश मानें।',
	consequence_unknown: 'एक नियोजित कॉरिडोर कार्रवाई चल रही है — छोटे ठहराव संभावित हैं।',
	card_hold_unknown: 'ठहराव की अवधि अभी घोषित नहीं हुई है।',
	card_hold_from_policy: 'ठहराव की अवधि घटना की क्लोज़र योजना से',
	card_hold_from_adaptive: 'ठहराव की अवधि लाइव ट्रैफ़िक के अनुसार समायोजित',
	card_hold_from_baseline: 'ठहराव की मानक अवधि — अभी लाइव ट्रैफ़िक के अनुसार समायोजित नहीं',
	card_hold_unavailable: 'ठहराव की अवधि अभी उपलब्ध नहीं — एक मिनट बाद फिर देखें।',
	card_alt_none: 'मॉडल किए गए मार्गों पर कोई चिह्नित विकल्प नहीं — अतिरिक्त समय रखें।',
	card_alt_unavailable: 'वैकल्पिक मार्ग अभी उपलब्ध नहीं — एक मिनट बाद फिर देखें।',
	card_alt_from_graph: 'विकल्प की गणना शहर के सड़क-ग्राफ़ पर की गई',
	citizen_feed_offline: 'लाइव सलाह अभी उपलब्ध नहीं — चिह्नित ऑफ़लाइन पूर्वावलोकन दिख रहा है।',
	citizen_feed_connecting: 'ट्रैफ़िक कंट्रोल रूम से जुड़ रहे हैं…',
	citizen_feed_stale: 'लाइव सलाह कुछ देर के लिए उपलब्ध नहीं — पिछला अपडेट दिख रहा है।',
	citizen_none_now: 'अगले २ घंटों में कोई कॉरिडोर घटना नहीं।',
	citizen_preview_none: 'पूर्वावलोकन के लिए अभी कोई सार्वजनिक कॉरिडोर घटना नहीं।',
	map_unavailable: 'नक्शा उपलब्ध नहीं',
	map_unavailable_hint: 'इस डिवाइस पर नक्शा शुरू नहीं हो सका।',

	// ── /driver ─────────────────────────────────────────────────────────
	driver_kicker: 'ग्रीन कॉरिडोर',
	golden_hour: 'गोल्डन आवर',
	golden_elapsed: 'बीत चुका',
	phase_idle: 'निष्क्रिय',
	phase_evaluating: 'आँका जा रहा है',
	phase_proposal: 'प्रस्ताव',
	phase_open: 'विंडो खुली',
	phase_deferred: 'टाला गया',
	doctrine_note: 'ग्रीन कॉरिडोर एम्बुलेंस के चौराहे पहुँचने से ६०–१८० सेकंड पहले खुलता है।',
	scripted_badge: 'स्क्रिप्टेड फ़ॉलबैक',
	scripted_note:
		'अभी कोई लाइव कॉरिडोर घटना नहीं — यह चिह्नित स्क्रिप्टेड फ़ॉलबैक है: कुछ नहीं भेजा जाता और सभी आँकड़े डेमो-स्केल हैं।',
	corridor_map: 'कॉरिडोर नक्शा',
	corridor_granted_stretch: 'स्वीकृत हिस्सा',
	unit_en_route: 'जन रक्षक यूनिट रास्ते में',
	unit_on_scene: 'जन रक्षक यूनिट मौके पर',
	units_note_fallback: 'स्क्रिप्टेड फ़ॉलबैक — यह यूनिट की चाल सिम्युलेटेड है, लाइव डिस्पैच नहीं।',
	control_room: 'कंट्रोल रूम',
	control_room_line: '108 कंट्रोल · चैनल पर बने रहें',
	after_action: (n) => `दर्ज ५–२० मिनट के ठहराव की तुलना में ~${n} मिनट बचे`,
	map_note_fallback: 'सड़क ज्यामिति उपलब्ध नहीं — कॉरिडोर रेखाएँ नहीं बनाई गईं।',

	driver_unit_label: 'एम्बुलेंस यूनिट',
	driver_unit_hint: 'आपका 108 यूनिट नंबर',
	driver_unit_invalid: 'अपना यूनिट नंबर लिखें, जैसे 108-14।',
	driver_stretch_option: (road, seg) => `${road} · हिस्सा ${seg}`,
	driver_event_label: (kind) => `घटना: ${kind}`,
	phase_requested: 'ड्यूटी अधिकारी की प्रतीक्षा',
	phase_approved: 'मंज़ूर',
	driver_awaiting_title: 'ड्यूटी अधिकारी की प्रतीक्षा',
	driver_awaiting_body:
		'आपका अनुरोध कंट्रोल-रूम की दीवार पर है। विंडो तभी खुलेगी जब कोई नामित ड्यूटी अधिकारी उसे मंज़ूर करेगा।',
	driver_request_ref: (id) => `अनुरोध ${id}`,
	driver_deferred_body: 'कंट्रोल रूम ने यह अनुरोध टाल दिया — अपनी जगह रुकें और ज़रूरत हो तो फिर अनुरोध करें।',
	driver_cancelled_body: 'कंट्रोल रूम ने यह अनुरोध रद्द कर दिया।',
	driver_primed_title: 'मंज़ूर — कॉरिडोर तैयार किया जा रहा है',
	driver_primed_countdown: (sec) => `विंडो ${sec} सेकंड में खुलेगी`,
	driver_primed_when: (clock) => `${clock} बजे — जंक्शन पर आपके पहुँचने के समय के अनुसार`,
	driver_window_from_policy: (sec) => `विंडो ${sec} सेकंड — घटना की क्लोज़र योजना से`,
	driver_window_from_adaptive: (sec) => `विंडो ${sec} सेकंड — लाइव ट्रैफ़िक के अनुसार समायोजित`,
	driver_window_from_baseline: (sec) => `विंडो ${sec} सेकंड — मानक अवधि, अभी लाइव ट्रैफ़िक के अनुसार समायोजित नहीं`,
	driver_window_clamped: (raw) => `(घटना का मान ${raw} सेकंड, ६०–१८० सेकंड के नियम में रखा गया)`,
	driver_window_anchor_eta: 'यूनिट के जंक्शन पहुँचने से पहले खुलती है, पहुँचते ही बंद होती है',
	driver_window_anchor_approval: 'अधिकारी की मंज़ूरी से गिनी जाती है — यूनिट का आगमन अनुमानित नहीं',
	driver_window_unpublished: 'इस घटना के लिए विंडो की अवधि घोषित नहीं — 108 कंट्रोल के निर्देश मानें।',
	driver_unit_eta: (sec) => `यूनिट जंक्शन से ~${sec} सेकंड दूर`,
	driver_unit_arrived: 'यूनिट जंक्शन पर',
	driver_unit_no_position: 'यूनिट की स्थिति अभी अनुमानित नहीं',
	driver_unit_emulated: 'सिम्युलेटेड यूनिट (EMULATED)',
	driver_window_closed: 'विंडो बंद — कॉरिडोर अपने नियोजित ठहराव पर लौटता है।',
	driver_request_failed: (msg) => `अनुरोध नहीं भेजा गया — ${msg}`,
	driver_status_retry: 'स्थिति फ़ीड उपलब्ध नहीं — फिर कोशिश हो रही है…',
	driver_live_note:
		'लाइव: अनुरोध, अधिकारी का निर्णय और हर स्थिति-परिवर्तन डिस्पैच ऑडिट में दर्ज होता है।',
	driver_scripted_approval: 'स्क्रिप्टेड मंज़ूरी — कोई ड्यूटी अधिकारी शामिल नहीं (केवल फ़ॉलबैक)।',

	dispatch_status_requested: 'अनुरोधित',
	dispatch_status_approved: 'मंज़ूर',
	dispatch_status_en_route: 'रास्ते में',
	dispatch_status_on_scene: 'मौके पर',
	dispatch_status_completed: 'पूर्ण',
	dispatch_status_deferred: 'टाला गया',
	dispatch_status_cancelled: 'रद्द',

	// ── /comms ──────────────────────────────────────────────────────────
	comms_title: 'सलाह डेस्क',
	comms_sub: 'एक तथ्य, हर श्रोता, हर भाषा — अधिकारी के भेजने के लिए लिखा जाता है, स्वतः नहीं भेजा जाता।',
	comms_pick_event: 'घटना',
	comms_generate: 'सलाह तैयार करें',
	comms_regenerate: 'फिर तैयार करें',
	comms_copy: 'कॉपी',
	comms_copied: 'कॉपी हो गया',
	comms_audience: 'श्रोता',
	audience_citizens: 'नागरिक · SMS / व्हाट्सऐप',
	audience_zones: 'ज़ोन अधिकारी',
	audience_108: '108 कंट्रोल',
	audience_municipal: 'नगरपालिका',
	comms_preview_note: 'पूर्वावलोकन — हर श्रोता को मिलने वाला ठीक यही पाठ है।',
	comms_template_title: 'घटना-प्रकार की टेम्पलेट',
	comms_draft_log: 'भेजने / ड्राफ़्ट का रिकॉर्ड',
	comms_drafted_badge: 'लिखा गया — नहीं भेजा गया',
	comms_no_gateway:
		'कोई संदेश गेटवे तैनात नहीं है। ड्राफ़्ट यहाँ अधिकारी के कॉपी करके भेजने के लिए बनते हैं — कुछ भी डिलीवर या ऑडिट रिकॉर्ड में दर्ज नहीं होता।',
	comms_dispatch_threads: 'डिस्पैच थ्रेड',
	comms_dispatch_live: 'लाइव',
	comms_dispatch_demo: 'स्क्रिप्टेड डेमो',
	comms_dispatch_none: 'इस घटना के लिए अभी कोई डिस्पैच दर्ज नहीं।',
	comms_all_clear: 'सभी-साफ़ ड्राफ़्ट करें',
	comms_session_only: 'ड्राफ़्ट केवल इस ब्राउज़र टैब में रहते हैं — न भेजे जाते हैं, न ऑडिट रिकॉर्ड में लिखे जाते हैं।',
	comms_agencies: 'एजेंसियाँ',
	comms_not_deployed: 'तैनात नहीं',
	comms_scope_event: 'यह घटना',
	comms_scope_all: 'सभी घटनाएँ',
	comms_other_events: (n) => `${n} अन्य घटनाओं पर`,
	comms_withheld: 'केवल-ऑपरेटर घटना प्रकार — इसके लिए नागरिक सलाह नहीं बनाई जाती।',
	comms_template_hint: 'नागरिक टेम्पलेट देखने के लिए प्रकार चुनें।',

	adv_citizen_hold: (kind, minutes, clock, alt) =>
		`${kind}: ${clock} से मार्ग पर ~${minutes} मिनट का ठहराव। ${alt} इस्तेमाल करें। सड़कें समय पर खुलेंगी।`,
	adv_citizen_hold_noalt: (kind, minutes, clock) =>
		`${kind}: ${clock} से मार्ग पर ~${minutes} मिनट का ठहराव। कोई चिह्नित विकल्प नहीं — अतिरिक्त समय रखें। सड़कें समय पर खुलेंगी।`,
	adv_citizen_hold_altunknown: (kind, minutes, clock) =>
		`${kind}: ${clock} से मार्ग पर ~${minutes} मिनट का ठहराव। अतिरिक्त समय रखें। सड़कें समय पर खुलेंगी।`,
	adv_citizen_clear: (kind) => `${kind} समाप्त। सड़कें समय पर खुल गई हैं। आपके सहयोग के लिए धन्यवाद।`,
	adv_zone: (kind, seg, minutes) =>
		`ज़ोन अधिकारी: ${kind} — हिस्सा ${seg} पर ~${minutes} मिनट ठहराव। रोकें, फिर समय पर छोड़ें।`,
	adv_108: (kind, seg) =>
		`108 कंट्रोल: ${kind} हिस्सा ${seg} पर सक्रिय। इस हिस्से के ग्रीन-कॉरिडोर अनुरोधों का उत्तर दिया जा रहा है।`,
	adv_municipal: (kind, seg, minutes) =>
		`नगरपालिका: ${kind} हिस्सा ${seg} पर, ~${minutes} मिनट के नियोजित ठहराव। बैरिकेड टीमें चौराहा-दर-चौराहे छोड़े।`,

	// ── /chat ───────────────────────────────────────────────────────────
	chat_title: 'एजेंट कंसोल',
	chat_sub: 'कॉरिडोर ब्रेन · चयनित घटना पर स्ट्रीम्ड तर्क',
	chat_context: 'संदर्भ',
	chat_context_none: 'कोई घटना चयनित नहीं — कंसोल बिना संदर्भ के चलेगा।',
	chat_placeholder: 'कॉरिडोर के बारे में पूछें…',
	chat_send: 'भेजें',
	chat_clear: 'साफ़ करें',
	chat_offline: 'एजेंट ऑफ़लाइन',
	chat_offline_note: 'एजेंट स्ट्रीम उत्तर नहीं दे रही — लौटने तक कंसोल केवल-पढ़ने के लिए है।',
	chat_thoughts: 'तर्क',
	chat_tools: 'टूल कॉल',
	chat_you: 'आप',
	chat_agent: 'एजेंट',
	chat_tool_pending: 'लंबित',
	chat_tool_running: 'चल रहा है',
	chat_tool_success: 'पूर्ण',
	chat_tool_error: 'विफल',
	chat_tiers: 'स्तर',
	chat_tier_decider: 'निर्णायक',
	chat_tier_gemma: 'जेम्मा',
	chat_tier_heuristic: 'ह्यूरिस्टिक',
	chat_degraded_turn: 'सीमित उत्तर',
	chat_raw_envelope: 'कोई गद्य उत्तर नहीं — ब्रेन ने कच्चा JSON लिफ़ाफ़ा लौटाया (नीचे जस का तस)।',
	chat_alerts: 'ऑपरेटर अलर्ट',
	chat_session: 'सत्र',
	chat_action: 'कार्य',

	// ── wave 3: provenance, the 2 h horizon, event phases ───────────────
	citizen_emulated_badge: 'सिम्युलेटेड फ़ीड · EMULATED',
	citizen_emulated_note: 'SIMULATED FEED वाले कार्ड इवेंट-एम्युलेटर का प्रदर्शन हैं — असली सड़क ठहराव नहीं।',
	citizen_later_title: 'बाद में — अगले २ घंटों के बाद',
	citizen_later_note: 'ठहराव का समय शुरुआत के क़रीब घोषित होगा।',
	citizen_later_unscheduled: 'समय तय नहीं',
	citizen_later_roads_withheld: 'मार्ग शुरुआत से २ घंटे के भीतर घोषित होगा',
	card_sim_now: 'सिम्युलेटेड · अभी',
	adv_emulated_prefix: '[सिम्युलेटेड फ़ीड · EMULATED — अभ्यास पाठ, असली सड़क ठहराव नहीं]',
	card_sim_soon: 'सिम्युलेटेड · अगला',
	consequence_planned_protected_movement: 'एक सुरक्षित काफ़िला गुज़रेगा — हर चौराहा पार होते ही ठहराव हटेंगे।',
	consequence_planned_procession: 'जुलूस मार्ग पर चलेगा — गुज़रने तक क्रॉसिंग थोड़ी देर बंद रहेंगी।',
	consequence_planned_mass_gathering: 'बड़ी भीड़ छँटेगी — भीड़ की सुरक्षा के लिए यातायात नियंत्रित किया जाएगा।',
	consequence_planned_race: 'सड़क दौड़ होगी — आख़िरी धावक के निकलने तक हिस्सा बंद रहेगा।',
	consequence_planned_ceremonial: 'औपचारिक परेड गुज़रेगी — केवल छोटे क्रमिक ठहराव।',
	consequence_planned_egress_surge: 'स्टेडियम की भीड़ निकलेगी — निकास लहरों में नियंत्रित होंगे।',
	consequence_planned_emergency_evacuation: 'राहत और आपात यातायात को प्राथमिकता मिलेगी — आम यातायात थोड़ी देर रुकेगा।',
	consequence_planned_work_zone: 'सड़क कार्य से मार्ग संकरा होगा — यातायात एक लेन से गुज़रेगा।',
	consequence_planned_unknown: 'एक नियोजित कॉरिडोर अभियान चलेगा — छोटे ठहराव संभव।',
	phase_closure: 'बंदी',
	phase_works: 'सड़क कार्य',
	phase_setup: 'तैयारी',
	phase_procession: 'जुलूस',
	phase_assembly: 'जमावड़ा',
	phase_approach: 'आगमन',
	phase_transit: 'पारगमन',
	phase_egress: 'निकास',
	phase_dispersal: 'भीड़ छँटना',
	phase_evacuation: 'निकासी',
	phase_parade: 'परेड',
	phase_race: 'दौड़',
	phase_movement_window: 'काफ़िले की अवधि',
	phase_generic: (n) => `चरण ${n}`,
	driver_new_request: 'नया अनुरोध',
	driver_emulated_hidden: (n) =>
		`${n} सिम्युलेटेड (EMULATED) कॉरिडोर घटनाएँ नहीं दिखाई गईं — अनुरोध केवल असली कॉरिडोर पर हो सकता है।`,
};

// ─── GU ─────────────────────────────────────────────────────────────────────

const gu: Pack = {
	// ── /citizen ────────────────────────────────────────────────────────
	citizen_kicker: 'તમારો રસ્તો, આગામી ૨ કલાક',
	citizen_explainer_link: 'રસ્તા કેમ રોકાય છે',
	strip_calm: 'અત્યારે કોઈ કોરિડોર અવરોધ જાહેર નથી.',
	strip_watch: 'એક ઘટના માર્ગ સક્રિય છે — તેની નજીકે ટૂંકા આયોજિત અવરોધ શક્ય છે.',
	strip_hold: 'નીચેના માર્ગો પર આયોજિત અવરોધ ચાલુ છે. સમયસર ખૂલશે.',
	strip_severe: 'પ્રાથમિકતાવાળો કોરિડોર ટ્રાફિક ચાલુ છે. સામાન્ય અવરોધ ટૂંકા રહે છે અને ચોકડી-પ્રતિ-ચોકડી ખૂલે છે.',
	card_hold_label: 'રસ્તો બંધ રહેશે',
	card_window_at: (clock) => `આસપાસ ${clock} વાગ્યે`,
	card_now: 'અત્યારે બંધ',
	card_soon: 'આગામી',
	card_alt_label: 'આ બદલે ચાલો',
	timeline_title: 'આજે તમારા માર્ગ પર',
	timeline_empty: 'આજે બીજી કોઈ કોરિડોર ઘટના આયોજિત નથી.',
	timeline_lifted: 'હટી ગયો',
	timeline_planned: 'આયોજિત',
	subscribe_demo_note: 'ડેમો પ્રોટોટાઇપ — આ બિલ્ડમાંથી કોઈ સંદેશ મોકલાશે નહીં.',
	footer_privacy: 'કોઈ લોકેશન લેવાતું નથી. સલાહમાં ફક્ત માર્ગ અને સમય હોય છે.',

	consequence_protected_movement:
		'સંરક્ષિત હલનચલન પસાર થાય છે — જેમ જેમ ચોકડી પાર કરે, અવરોધ ખૂલતો જાય છે.',
	consequence_procession: 'રથયાત્રા માર્ગ પર ચાલે છે — પસાર થાય ત્યાં સુધી ક્રોસિંગ થોડીવાર બંધ રહે છે.',
	consequence_mass_gathering: 'મોટી ભીડ વિખરાય છે — સુરક્ષા માટે ટ્રાફિક થોડું-થોડું છોડાય છે.',
	consequence_race: 'રોડ રેસ ચાલુ છે — છેલ્લો દોડવીર નીકળે ત્યાં સુધી માર્ગ બંધ રહેશે.',
	consequence_ceremonial: 'એક પરેડ પસાર થાય છે — ફક્ત ટૂંકા ફરતા અવરોધ.',
	consequence_egress_surge: 'સ્ટેડિયમમાંથી ભીડ પરત આવે છે — નિકાસ મોજાંમાં થાય છે.',
	consequence_emergency_evacuation:
		'રાહત અને કટોકટી ટ્રાફિકને પ્રાથમિકતા છે — સામાન્ય ટ્રાફિક થોડીવાર રોકાય છે.',
	consequence_work_zone: 'રસ્તા કામથી માર્ગ સાંકડો છે — એક સમયે એક લેન.',
	consequence_vehicle_pursuit: 'અહીં પોલીસ કાર્યવાહી ચાલુ છે — અધિકારીઓની સૂચનાઓનું પાલન કરો.',
	consequence_crime_incident: 'અહીં ઘટના પર કાર્યવાહી ચાલુ છે — અધિકારીઓની સૂચનાઓનું પાલન કરો.',
	consequence_unknown: 'એક આયોજિત કોરિડોર કાર્યવાહી ચાલુ છે — ટૂંકા અવરોધ શક્ય છે.',
	card_hold_unknown: 'અવરોધનો સમયગાળો હજી જાહેર થયો નથી.',
	card_hold_from_policy: 'અવરોધનો સમયગાળો ઘટનાની ક્લોઝર યોજનામાંથી',
	card_hold_from_adaptive: 'અવરોધનો સમયગાળો લાઇવ ટ્રાફિક મુજબ ગોઠવાયેલો',
	card_hold_from_baseline: 'અવરોધનો પ્રમાણભૂત સમયગાળો — હજી લાઇવ ટ્રાફિક મુજબ ગોઠવાયો નથી',
	card_hold_unavailable: 'અવરોધનો સમયગાળો અત્યારે ઉપલબ્ધ નથી — એક મિનિટ પછી ફરી જુઓ.',
	card_alt_none: 'મોડેલ કરેલા રસ્તાઓ પર કોઈ ચિહ્નિત વિકલ્પ નથી — વધારાનો સમય રાખો.',
	card_alt_unavailable: 'વૈકલ્પિક રસ્તો અત્યારે ઉપલબ્ધ નથી — એક મિનિટ પછી ફરી જુઓ.',
	card_alt_from_graph: 'વિકલ્પ શહેરના રસ્તા-ગ્રાફ પર ગણાયો',
	citizen_feed_offline: 'લાઇવ સલાહ અત્યારે ઉપલબ્ધ નથી — ચિહ્નિત ઑફલાઇન પ્રીવ્યૂ દેખાય છે.',
	citizen_feed_connecting: 'ટ્રાફિક કંટ્રોલ રૂમ સાથે જોડાઈ રહ્યા છીએ…',
	citizen_feed_stale: 'લાઇવ સલાહ થોડી વાર માટે ઉપલબ્ધ નથી — છેલ્લું અપડેટ દેખાય છે.',
	citizen_none_now: 'આગામી ૨ કલાકમાં કોઈ કોરિડોર ઘટના નથી.',
	citizen_preview_none: 'પ્રીવ્યૂ માટે અત્યારે કોઈ જાહેર કોરિડોર ઘટના નથી.',
	map_unavailable: 'નકશો ઉપલબ્ધ નથી',
	map_unavailable_hint: 'આ ઉપકરણ પર નકશો શરૂ થઈ શક્યો નથી.',

	// ── /driver ─────────────────────────────────────────────────────────
	driver_kicker: 'ગ્રીન કોરિડોર',
	golden_hour: 'ગોલ્ડન આવર',
	golden_elapsed: 'વીત્યો',
	phase_idle: 'નિષ્ક્રિય',
	phase_evaluating: 'મૂલવાઈ રહ્યું',
	phase_proposal: 'પ્રસ્તાવ',
	phase_open: 'વિન્ડો ખુલી',
	phase_deferred: 'ટાળેલ',
	doctrine_note: 'એમ્બ્યુલન્સ ચોકડી પર પહોંચે તેના ૬૦–૧૮૦ સેકંડ પહેલાં ગ્રીન કોરિડોર ખૂલે છે.',
	scripted_badge: 'સ્ક્રિપ્ટેડ ફૉલબેક',
	scripted_note:
		'અત્યારે કોઈ લાઇવ કોરિડોર ઘટના નથી — આ ચિહ્નિત સ્ક્રિપ્ટેડ ફૉલબેક છે: કંઈ મોકલાતું નથી અને બધા આંકડા ડેમો-સ્કેલ છે.',
	corridor_map: 'કોરિડોર નકશો',
	corridor_granted_stretch: 'મંજૂર ટુકડો',
	unit_en_route: 'જન રક્ષક યુનિટ રસ્તામાં',
	unit_on_scene: 'જન રક્ષક યુનિટ સ્થળ પર',
	units_note_fallback: 'સ્ક્રિપ્ટેડ ફૉલબેક — આ યુનિટની ચાલ સિમ્યુલેટેડ છે, લાઇવ ડિસ્પેચ નથી.',
	control_room: 'કંટ્રોલ રૂમ',
	control_room_line: '108 કંટ્રોલ · ચેનલ પર રહો',
	after_action: (n) => `નોંધાયેલા ૫–૨૦ મિનિટના અવરોધ સામે ~${n} મિનિટ બચી`,
	map_note_fallback: 'રસ્તાની ભૂમિતિ ઉપલબ્ધ નથી — કોરિડોર રેખાઓ દોરાઈ નથી.',

	driver_unit_label: 'એમ્બ્યુલન્સ યુનિટ',
	driver_unit_hint: 'તમારો 108 યુનિટ નંબર',
	driver_unit_invalid: 'તમારો યુનિટ નંબર લખો, જેમ કે 108-14.',
	driver_stretch_option: (road, seg) => `${road} · ટુકડો ${seg}`,
	driver_event_label: (kind) => `ઘટના: ${kind}`,
	phase_requested: 'ડ્યુટી ઓફિસરની રાહ',
	phase_approved: 'મંજૂર',
	driver_awaiting_title: 'ડ્યુટી ઓફિસરની રાહ',
	driver_awaiting_body:
		'તમારી વિનંતી કંટ્રોલ-રૂમની દીવાલ પર છે. નામાંકિત ડ્યુટી ઓફિસર મંજૂર કરે ત્યારે જ વિન્ડો ખૂલશે.',
	driver_request_ref: (id) => `વિનંતી ${id}`,
	driver_deferred_body: 'કંટ્રોલ રૂમે આ વિનંતી ટાળી — તમારી જગ્યાએ રોકાઓ અને જરૂર હોય તો ફરી વિનંતી કરો.',
	driver_cancelled_body: 'કંટ્રોલ રૂમે આ વિનંતી રદ કરી.',
	driver_primed_title: 'મંજૂર — કોરિડોર તૈયાર થઈ રહ્યો છે',
	driver_primed_countdown: (sec) => `વિન્ડો ${sec} સેકંડમાં ખૂલશે`,
	driver_primed_when: (clock) => `${clock} વાગ્યે — જંક્શન પર તમારા પહોંચવાના સમય મુજબ`,
	driver_window_from_policy: (sec) => `વિન્ડો ${sec} સેકંડ — ઘટનાની ક્લોઝર યોજનામાંથી`,
	driver_window_from_adaptive: (sec) => `વિન્ડો ${sec} સેકંડ — લાઇવ ટ્રાફિક મુજબ ગોઠવાયેલી`,
	driver_window_from_baseline: (sec) => `વિન્ડો ${sec} સેકંડ — પ્રમાણભૂત સમયગાળો, હજી લાઇવ ટ્રાફિક મુજબ ગોઠવાયો નથી`,
	driver_window_clamped: (raw) => `(ઘટનાનું મૂલ્ય ${raw} સેકંડ, ૬૦–૧૮૦ સેકંડના નિયમમાં રાખ્યું)`,
	driver_window_anchor_eta: 'યુનિટ જંક્શન પર પહોંચે તે પહેલાં ખૂલે છે, પહોંચતાં જ બંધ થાય છે',
	driver_window_anchor_approval: 'ઓફિસરની મંજૂરીથી ગણાય છે — યુનિટનું આગમન અંદાજાયું નથી',
	driver_window_unpublished: 'આ ઘટના માટે વિન્ડોનો સમયગાળો જાહેર નથી — 108 કંટ્રોલની સૂચના મુજબ ચાલો.',
	driver_unit_eta: (sec) => `યુનિટ જંક્શનથી ~${sec} સેકંડ દૂર`,
	driver_unit_arrived: 'યુનિટ જંક્શન પર',
	driver_unit_no_position: 'યુનિટનું સ્થાન હજી અંદાજાયું નથી',
	driver_unit_emulated: 'સિમ્યુલેટેડ યુનિટ (EMULATED)',
	driver_window_closed: 'વિન્ડો બંધ — કોરિડોર તેના આયોજિત અવરોધ પર પાછો ફરે છે.',
	driver_request_failed: (msg) => `વિનંતી મોકલાઈ નથી — ${msg}`,
	driver_status_retry: 'સ્થિતિ ફીડ ઉપલબ્ધ નથી — ફરી પ્રયાસ ચાલુ છે…',
	driver_live_note:
		'લાઇવ: વિનંતી, ઓફિસરનો નિર્ણય અને દરેક સ્થિતિ-ફેરફાર ડિસ્પેચ ઑડિટમાં નોંધાય છે.',
	driver_scripted_approval: 'સ્ક્રિપ્ટેડ મંજૂરી — કોઈ ડ્યુટી ઓફિસર સામેલ નથી (ફક્ત ફૉલબેક).',

	dispatch_status_requested: 'વિનંતી કરેલ',
	dispatch_status_approved: 'મંજૂર',
	dispatch_status_en_route: 'રસ્તામાં',
	dispatch_status_on_scene: 'સ્થળ પર',
	dispatch_status_completed: 'પૂર્ણ',
	dispatch_status_deferred: 'ટાળેલ',
	dispatch_status_cancelled: 'રદ',

	// ── /comms ──────────────────────────────────────────────────────────
	comms_title: 'સલાહ ડેસ્ક',
	comms_sub: 'એક હકીકત, દરેક શ્રોતા, દરેક ભાષા — અધિકારી મોકલે તે માટે લખાય, આપોઆપ મોકલાય નહીં.',
	comms_pick_event: 'ઘટના',
	comms_generate: 'સલાહ તૈયાર કરો',
	comms_regenerate: 'ફરી તૈયાર કરો',
	comms_copy: 'કૉપી',
	comms_copied: 'કૉપી થયું',
	comms_audience: 'શ્રોતા',
	audience_citizens: 'નાગરિકો · SMS / વ્હોટ્સએપ',
	audience_zones: 'ઝોન અધિકારીઓ',
	audience_108: '108 કંટ્રોલ',
	audience_municipal: 'મ્યુનિસિપાલિટી',
	comms_preview_note: 'પ્રીવ્યૂ — દરેક શ્રોતાને મળનાર બરાબર આ જ લખાણ છે.',
	comms_template_title: 'ઘટના-પ્રકારની ટેમ્પલેટ',
	comms_draft_log: 'મોકલ / ડ્રાફ્ટ રેકોર્ડ',
	comms_drafted_badge: 'લખાયું — મોકલાયું નથી',
	comms_no_gateway:
		'કોઈ મેસેજ ગેટવે ડિપ્લોય નથી. ડ્રાફ્ટ અહીં અધિકારી કૉપી કરીને મોકલે તે માટે તૈયાર થાય છે — કંઈ ડિલિવર થતું નથી કે ઑડિટ રેકોર્ડમાં લખાતું નથી.',
	comms_dispatch_threads: 'ડિસ્પેચ થ્રેડ',
	comms_dispatch_live: 'લાઇવ',
	comms_dispatch_demo: 'સ્ક્રિપ્ટેડ ડેમો',
	comms_dispatch_none: 'આ ઘટના માટે હજુ કોઈ ડિસ્પેચ નોંધાયેલ નથી.',
	comms_all_clear: 'ઑલ-ક્લિયર ડ્રાફ્ટ કરો',
	comms_session_only: 'ડ્રાફ્ટ ફક્ત આ બ્રાઉઝર ટૅબમાં રહે છે — ન મોકલાય છે, ન ઑડિટ રેકોર્ડમાં લખાય છે.',
	comms_agencies: 'એજન્સીઓ',
	comms_not_deployed: 'ડિપ્લોય નથી',
	comms_scope_event: 'આ ઘટના',
	comms_scope_all: 'બધી ઘટનાઓ',
	comms_other_events: (n) => `${n} અન્ય ઘટનાઓ પર`,
	comms_withheld: 'ફક્ત-ઓપરેટર ઘટના પ્રકાર — તેના માટે નાગરિક સલાહ તૈયાર થતી નથી.',
	comms_template_hint: 'નાગરિક ટેમ્પલેટ જોવા માટે પ્રકાર પસંદ કરો.',

	adv_citizen_hold: (kind, minutes, clock, alt) =>
		`${kind}: ${clock} થી માર્ગ પર ~${minutes} મિનિટનો અવરોધ. ${alt} વાપરો. રસ્તા સમયસર ખૂલશે.`,
	adv_citizen_hold_noalt: (kind, minutes, clock) =>
		`${kind}: ${clock} થી માર્ગ પર ~${minutes} મિનિટનો અવરોધ. કોઈ ચિહ્નિત વિકલ્પ નથી — વધારાનો સમય રાખો. રસ્તા સમયસર ખૂલશે.`,
	adv_citizen_hold_altunknown: (kind, minutes, clock) =>
		`${kind}: ${clock} થી માર્ગ પર ~${minutes} મિનિટનો અવરોધ. વધારાનો સમય રાખો. રસ્તા સમયસર ખૂલશે.`,
	adv_citizen_clear: (kind) => `${kind} પૂર્ણ. રસ્તા સમયસર ખૂલી ગયા. તમારા સહકાર બદલ આભાર.`,
	adv_zone: (kind, seg, minutes) =>
		`ઝોન અધિકારીઓ: ${kind} — ટુકડો ${seg} પર ~${minutes} મિનિટ અવરોધ. રોકો, પછી સમયસર છોડો.`,
	adv_108: (kind, seg) =>
		`108 કંટ્રોલ: ${kind} ટુકડો ${seg} પર સક્રિય. આ ટુકડાની ગ્રીન-કોરિડોર વિનંતિઓના જવાબ અપાઈ રહ્યા છે.`,
	adv_municipal: (kind, seg, minutes) =>
		`મ્યુનિસિપાલિટી: ${kind} ટુકડો ${seg} પર, ~${minutes} મિનિટના આયોજિત અવરોધ. બેરિકેડ ટીમો ચોકડી-પ્રતિ-ચોકડી છોડે.`,

	// ── /chat ───────────────────────────────────────────────────────────
	chat_title: 'એજન્ટ કન્સોલ',
	chat_sub: 'કોરિડોર બ્રેઇન · પસંદ કરેલી ઘટના પર સ્ટ્રીમ્ડ તર્ક',
	chat_context: 'સંદર્ભ',
	chat_context_none: 'કોઈ ઘટના પસંદ નથી — કન્સોલ સંદર્ભ વિના ચાલશે.',
	chat_placeholder: 'કોરિડોર વિશે પૂછો…',
	chat_send: 'મોકલો',
	chat_clear: 'સાફ કરો',
	chat_offline: 'એજન્ટ ઑફલાઇન',
	chat_offline_note: 'એજન્ટ સ્ટ્રીમ જવાબ આપતી નથી — પરત આવે ત્યાં સુધી કન્સોલ ફક્ત-વાંચવાનો છે.',
	chat_thoughts: 'તર્ક',
	chat_tools: 'ટૂલ કૉલ',
	chat_you: 'તમે',
	chat_agent: 'એજન્ટ',
	chat_tool_pending: 'બાકી',
	chat_tool_running: 'ચાલુ',
	chat_tool_success: 'પૂર્ણ',
	chat_tool_error: 'નિષ્ફળ',
	chat_tiers: 'સ્તર',
	chat_tier_decider: 'નિર્ણયક',
	chat_tier_gemma: 'જેમ્મા',
	chat_tier_heuristic: 'હ્યુરિસ્ટિક',
	chat_degraded_turn: 'મર્યાદિત જવાબ',
	chat_raw_envelope: 'કોઈ ગદ્ય જવાબ નથી — બ્રેઇને કાચું JSON પરબિડીયું આપ્યું (નીચે જેમ છે તેમ).',
	chat_alerts: 'ઓપરેટર એલર્ટ',
	chat_session: 'સત્ર',
	chat_action: 'કાર્ય',

	// ── wave 3: provenance, the 2 h horizon, event phases ───────────────
	citizen_emulated_badge: 'સિમ્યુલેટેડ ફીડ · EMULATED',
	citizen_emulated_note: 'SIMULATED FEED વાળાં કાર્ડ ઇવેન્ટ-ઇમ્યુલેટરનું પ્રદર્શન છે — સાચા રસ્તા અવરોધ નહીં.',
	citizen_later_title: 'પછીથી — આગામી ૨ કલાક પછી',
	citizen_later_note: 'અવરોધનો સમય શરૂઆતની નજીક જાહેર થશે.',
	citizen_later_unscheduled: 'સમય નક્કી નથી',
	citizen_later_roads_withheld: 'માર્ગ શરૂઆતના ૨ કલાકની અંદર જાહેર થશે',
	card_sim_now: 'સિમ્યુલેટેડ · હમણાં',
	adv_emulated_prefix: '[સિમ્યુલેટેડ ફીડ · EMULATED — અભ્યાસ લખાણ, સાચો રસ્તા અવરોધ નહીં]',
	card_sim_soon: 'સિમ્યુલેટેડ · આગામી',
	consequence_planned_protected_movement: 'સુરક્ષિત કાફલો પસાર થશે — દરેક જંક્શન પાર થતાં અવરોધ હટશે.',
	consequence_planned_procession: 'રથયાત્રા માર્ગ પર ચાલશે — પસાર થાય ત્યાં સુધી ક્રોસિંગ થોડીવાર બંધ રહેશે.',
	consequence_planned_mass_gathering: 'મોટી ભીડ વિખેરાશે — ભીડની સલામતી માટે ટ્રાફિક નિયંત્રિત થશે.',
	consequence_planned_race: 'રોડ રેસ યોજાશે — છેલ્લો દોડવીર પસાર થાય ત્યાં સુધી રસ્તો બંધ રહેશે.',
	consequence_planned_ceremonial: 'ઔપચારિક પરેડ પસાર થશે — ફક્ત ટૂંકા ક્રમિક અવરોધ.',
	consequence_planned_egress_surge: 'સ્ટેડિયમની ભીડ નીકળશે — બહાર નીકળવાનું તબક્કાવાર નિયંત્રિત થશે.',
	consequence_planned_emergency_evacuation: 'રાહત અને કટોકટી ટ્રાફિકને પ્રાથમિકતા મળશે — સામાન્ય ટ્રાફિક થોડીવાર રોકાશે.',
	consequence_planned_work_zone: 'રસ્તાનું કામ માર્ગ સાંકડો કરશે — ટ્રાફિક એક લેનમાંથી પસાર થશે.',
	consequence_planned_unknown: 'આયોજિત કોરિડોર કામગીરી ચાલશે — ટૂંકા અવરોધ શક્ય.',
	phase_closure: 'બંધ',
	phase_works: 'રસ્તાનું કામ',
	phase_setup: 'તૈયારી',
	phase_procession: 'સરઘસ',
	phase_assembly: 'એકત્રીકરણ',
	phase_approach: 'આગમન',
	phase_transit: 'પસાર',
	phase_egress: 'બહાર નીકળવું',
	phase_dispersal: 'વિખેરાવું',
	phase_evacuation: 'સ્થળાંતર',
	phase_parade: 'પરેડ',
	phase_race: 'દોડ',
	phase_movement_window: 'કાફલાનો સમય',
	phase_generic: (n) => `તબક્કો ${n}`,
	driver_new_request: 'નવી વિનંતી',
	driver_emulated_hidden: (n) =>
		`${n} સિમ્યુલેટેડ (EMULATED) કોરિડોર ઘટના બતાવી નથી — વિનંતી ફક્ત સાચા કોરિડોર પર થઈ શકે.`,
};

// ─── Registration + resolution ───────────────────────────────────────────────

const CITIZEN_PACKS: Record<string, Pack> = { en, hi, gu };

registerPack('en', en);
registerPack('hi', hi);
registerPack('gu', gu);

/**
 * Citizen-screens resolver: this pack first (per language, EN fallback),
 * then the frozen core pack — components never call two resolvers.
 */
export function citizenT(lang: string, key: string, ...args: unknown[]): string {
	const pack = CITIZEN_PACKS[lang] ?? CITIZEN_PACKS.en;
	const val = pack[key] ?? CITIZEN_PACKS.en[key];
	if (val != null) {
		return typeof val === 'function' ? String(val(...args)) : String(val);
	}
	return t(lang, key, ...args);
}

/**
 * Plain-language consequence for an event kind, with a fallback: the
 * generic wording when a kind has no copy — never the raw key. A PLANNED
 * event (not active yet) reads in the future tense: nothing is holding
 * the road yet, and the copy must not say it is.
 */
export function consequenceLabel(lang: string, kind: string | null | undefined, planned = false): string {
	const key = `consequence_${planned ? 'planned_' : ''}${kind ?? ''}`;
	const val = citizenT(lang, key);
	if (val !== key) return val;
	return citizenT(lang, planned ? 'consequence_planned_unknown' : 'consequence_unknown');
}

/** Event-phase labels the public copy knows (the oracle's phase vocabulary). */
const PHASE_KEYS = new Set([
	'closure',
	'works',
	'setup',
	'procession',
	'assembly',
	'approach',
	'transit',
	'egress',
	'dispersal',
	'evacuation',
	'parade',
	'race',
	'movement_window',
]);

/**
 * An event phase's label in the reader's language: a known label
 * ('closure', 'set-up', 'movement window' …) through citizenT('phase_<label>'),
 * anything else as the localized generic 'phase N' — never an English
 * operator string inside a Hindi or Gujarati page.
 */
export function phaseLabelText(lang: string, label: string | null | undefined, n: number): string {
	const norm = (label ?? '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
	const key = norm === 'set_up' ? 'setup' : norm === 'road_works' ? 'works' : norm;
	if (PHASE_KEYS.has(key)) return citizenT(lang, `phase_${key}`);
	return citizenT(lang, 'phase_generic', localizeNumber(n, lang));
}

/** Whether a key is provided by this pack (used by the completeness gate). */
export function isCitizenKey(key: string): boolean {
	return key in en;
}
