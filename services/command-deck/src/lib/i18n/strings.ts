/**
 * Trilingual string packs (EN / HI / GU) for the corridor showcase.
 *
 * The citizen/driver keys are ported from the legacy advisory layer
 * (frontend/src/i18n/advisoryStrings.js) so the integrated citizen view keeps
 * its copy; this pack EXTENDS them with the eight corridor-event kinds and the
 * ops-board panel vocabulary required by the generalized event model.
 *
 * Public-facing copy only; road names stay transliterated as on city signage.
 */

export const SUPPORTED = [
	{ code: 'en', label: 'English' },
	{ code: 'hi', label: 'हिंदी' },
	{ code: 'gu', label: 'ગુજરાતી' },
] as const;

export type LangCode = (typeof SUPPORTED)[number]['code'];

type Pack = Record<string, string | ((...args: unknown[]) => string)>;

const en: Pack = {
	// ── Event kinds (the eight corridor-event archetypes) ─────────────
	kind_protected_movement: 'Protected movement',
	kind_procession: 'Procession (rath yatra)',
	kind_mass_gathering: 'Mass gathering (Kumbh-class)',
	kind_race: 'Road race (marathon)',
	kind_ceremonial: 'Ceremonial parade',
	kind_egress_surge: 'Stadium egress surge',
	kind_emergency_evacuation: 'Flood evacuation / relief',
	kind_work_zone: 'Infrastructure work zone',
	// Operator-only kinds (watchlist-anpr, migration 010) — labelled in the
	// core so no surface ever renders the raw key; public surfaces never
	// receive these events at all (holds.ts PUBLIC_EVENT_KINDS).
	kind_vehicle_pursuit: 'Vehicle pursuit',
	kind_crime_incident: 'Crime incident',
	kind_unknown: 'Corridor event',

	// ── Crowd classes ──────────────────────────────────────────────────
	crowd_LOW: 'Low crowd',
	crowd_MODERATE: 'Moderate crowd',
	crowd_HIGH: 'High crowd',
	crowd_EXTREME: 'Extreme crowd',

	// ── Situation bands (operational codes stay in English) ───────────
	band_GREEN: 'GREEN · normal',
	band_AMBER: 'AMBER · watch',
	band_RED: 'RED · intervene',
	band_BLACK: 'BLACK · take over',

	// ── Ops board ─────────────────────────────────────────────────────
	opsTitle: 'Corridor Operations Board',
	opsSub: 'Concurrent corridor events · live decision audit · adaptive situation band',
	opsSourceLive: 'live',
	opsSourceMovements: 'movements API',
	opsSourceOffline: 'offline preview',
	opsNoEvents: 'No active corridor events',
	opsEventsApiMissing: 'events API not deployed yet — showing movements fallback + labelled offline preview',
	opsPhase: 'phase',
	opsWindow: 'window',
	opsRouteSpine: 'route spine',
	opsSelectEvent: 'Select',
	opsSelected: 'selected',
	opsLastUpdated: (ago) => `updated ${ago}s ago`,
	opsStale: 'stale',
	opsNever: 'waiting for first response',

	// ── Audit stream ──────────────────────────────────────────────────
	auditTitle: 'Decision-Audit Stream',
	auditSub: 'decider → Gemma agent → MCP tool calls, from the brain’s audit channels',
	auditLadder: (rung) => `channel: ${rung}`,
	auditRungWsEvent: 'WS /ws/event',
	auditRungWsConvoy: 'WS /ws/convoy',
	auditRungPoll: '2 s poll',
	auditRungUnavailable: 'unavailable',
	auditProbe: 'Probe the agent',
	auditProbeHint: 'Sends one NDJSON audit query to /chat/stream and streams the ladder live.',
	auditClear: 'clear',
	auditEmpty: 'No audit entries yet — the stream appends live decisions, band changes and tool calls.',

	// ── Situation band ────────────────────────────────────────────────
	// R9 — the band panel is the Active Movement Adaptation module's face.
	bandTitle: 'Active Movement Adaptation',
	bandSub: 'situation → hysteresis → policy floors → twin shadow-eval (fail-closed) → audited persistence',
	bandHistory: 'band history',
	bandSourceLog: 'situation_log',
	bandSourceStatus: 'adaptive status',
	bandAccumulated: 'client-accumulated',
	bandShadowEval: 'shadow eval',
	bandFloorApplied: 'floor applied',
	bandFailClosed: 'fail-closed',

	// ── Twin compare ──────────────────────────────────────────────────
	twinTitle: 'Twin Shadow-Eval Comparison',
	twinSub: 'baseline vs candidate closure policy on the seeded corridor twin',
	twinRun: 'Run comparison',
	twinRunning: 'Running twin…',
	twinBaseline: 'baseline',
	twinCandidate: 'candidate',
	twinAdvance: 'advance (s)',
	twinClosure: 'closure',
	twinDisruption: 'disruption (veh·h)',
	twinQueue: 'max queue (m)',
	twinClosureDur: 'closure duration (s)',
	twinTransit: 'priority transit (s)',
	twinRecovery: 'recovery (s)',
	twinAmbulance: 'ambulance delay (s)',
	twinDelta: 'Δ',
	twinSourceCompare: 'via /simulate/twin/compare',
	twinSourceDirect: 'compare endpoint not deployed — two explicit /simulate/twin arms',
	twinCached: 'cached — refresh on demand',
	twinReplications: (n) => `${n} replications`,

	// ── Emulator ──────────────────────────────────────────────────────
	emuTitle: 'Event Emulator',
	emuSub: 'cause-tape injection on loopback :8090',
	emuOnline: 'emulator online',
	emuOffline: 'emulator offline',
	emuCheck: 'check health',
	emuLaunch: 'stage use case',
	emuLaunchNote: 'Launch contract is owned by the emulator stream; failures surface verbatim.',

	// ── Citizen advisory (ported) ─────────────────────────────────────
	brand: 'Corridor Alerts · Ahmedabad',
	tagline: 'Know before the barricade goes up.',
	updated: 'Advisories last checked',
	statusActive: 'Corridor event in progress',
	statusActiveSub: 'Rolling holds of ~2–3 minutes per stretch — not a full freeze.',
	statusClear: 'No active corridor restrictions',
	statusClearSub: 'These advisories cover planned corridor holds only — not general traffic.',
	statusDone: 'Event completed — corridor released',
	statusDoneSub: 'Traffic returning to normal flow.',
	startDemo: 'Play a demo movement',
	freezeTitle: 'Where holds are expected',
	freezeZone: 'Stretch',
	freezeWindow: 'Hold window',
	freezeMinutes: (m) => `~${m} min`,
	alternate: 'Use instead',
	holdNow: 'Holding now',
	passed: 'Cleared',
	why: 'Why so short?',
	whyBody:
		'Closures are timed to the event’s live position (60–180 s ahead) using traffic forecasting — the corridor freezes only while the movement actually passes.',
	subscribeTitle: 'Get alerts on your phone',
	subscribeSub: 'SMS / WhatsApp, free, in your language.',
	phoneLabel: 'Mobile number',
	phonePlaceholder: '10-digit mobile',
	languageLabel: 'Alert language',
	subscribeBtn: 'Subscribe',
	demoBadge: 'Demo preview — no real SMS/WhatsApp is sent',
	previewTitle: 'You would receive:',
	subscribed: 'Subscribed (demo)',
	emergencyTitle: 'Driving an ambulance or fire vehicle?',
	emergencyBody:
		'Request a green-corridor window through the active event — the control room answers in seconds.',
	emergencyCta: 'Request green corridor',
	backToOps: 'Operator console',
	footerNote:
		'Corridor-event orchestration prototype. Alternate routes are indicative.',
	driverTitle: 'Green Corridor — 108 driver',
	driverSub: 'One tap. The control room answers in seconds.',
	driverBack: 'Citizen alerts',
	noMovement: 'No active corridor',
	noMovementSub:
		'No corridor event right now — drive normally. Pick a demo event above to try this flow.',
	atStretch: 'You are at',
	urgency: 'Patient urgency',
	urgCardiac: 'Cardiac',
	urgTrauma: 'Trauma',
	urgStroke: 'Stroke',
	urgOther: 'Other emergency',
	requestBtn: 'Request corridor window',
	evaluating: 'Evaluating corridor gap…',
	proposalTitle: 'Window proposed by the platform',
	convoyReachesIn: 'The event reaches your stretch in',
	crossBefore: 'Cross before',
	officerLabel: 'Duty officer (demo)',
	approveBtn: 'Approve · open window',
	deferBtn: 'Defer',
	windowOpen: 'WINDOW OPEN — CROSS NOW',
	windowClosesIn: 'Window closes in',
	minutesReturned: 'minutes returned to the golden hour',
	holdPosition: 'Hold position — next window at',
	baselineNote:
		'Without a request channel: documented ambulance holds behind convoy corridors run 5–20 minutes.',
	openDriverView: 'Open the 108 driver view',
	demoNote: 'Demo flow driven by the scripted simulation — no real dispatch, no real patient data.',
};

const hi: Pack = {
	kind_protected_movement: 'सुरक्षित वाहन काफ़िला',
	kind_procession: 'रथ यात्रा जुलूस',
	kind_mass_gathering: 'विशाल समागम (कुंभ-स्तर)',
	kind_race: 'सड़क दौड़ (मैराथन)',
	kind_ceremonial: 'औपचारिक परेड',
	kind_egress_surge: 'स्टेडियम निकासी भीड़',
	kind_emergency_evacuation: 'बाढ़ राहत / खाली करने का काफ़िला',
	kind_work_zone: 'बुनियादी कार्य क्षेत्र',
	kind_vehicle_pursuit: 'वाहन पर्सुट',
	kind_crime_incident: 'अपराध घटना',
	kind_unknown: 'कॉरिडोर घटना',

	crowd_LOW: 'कम भीड़',
	crowd_MODERATE: 'मध्यम भीड़',
	crowd_HIGH: 'भारी भीड़',
	crowd_EXTREME: 'अत्यंत भारी भीड़',

	band_GREEN: 'GREEN · सामान्य',
	band_AMBER: 'AMBER · सतर्क',
	band_RED: 'RED · हस्तक्षेप',
	band_BLACK: 'BLACK · नियंत्रण लें',

	opsTitle: 'कॉरिडोर ऑपरेशन बोर्ड',
	opsSub: 'एक साथ चल रही कॉरिडोर घटनाएँ · लाइव निर्णय ऑडिट · अनुकूली स्थिति बैंड',
	opsSourceLive: 'लाइव',
	opsSourceMovements: 'मूवमेंट्स API',
	opsSourceOffline: 'ऑफ़लाइन पूर्वावलोकन',
	opsNoEvents: 'कोई सक्रिय कॉरिडोर घटना नहीं',
	opsEventsApiMissing:
		'इवेंट्स API अभी तैनात नहीं — मूवमेंट्स फ़ॉलबैक + चिह्नित ऑफ़लाइन पूर्वावलोकन दिख रहा है',
	opsPhase: 'चरण',
	opsWindow: 'समय-खिड़की',
	opsRouteSpine: 'मार्ग स्तंभ',
	opsSelectEvent: 'चुनें',
	opsSelected: 'चयनित',
	opsLastUpdated: (ago) => `${ago} सेकंड पहले अपडेट`,
	opsStale: 'पुराना',
	opsNever: 'पहली प्रतिक्रिया की प्रतीक्षा',

	auditTitle: 'निर्णय-ऑडिट स्ट्रीम',
	auditSub: 'निर्णायक → जेम्मा एजेंट → MCP टूल कॉल, ब्रेन के ऑडिट चैनलों से',
	auditLadder: (rung) => `चैनल: ${rung}`,
	auditRungWsEvent: 'WS /ws/event',
	auditRungWsConvoy: 'WS /ws/convoy',
	auditRungPoll: '2 सेकंड पोल',
	auditRungUnavailable: 'अनुपलब्ध',
	auditProbe: 'एजेंट को जाँचें',
	auditProbeHint: '/chat/stream पर एक NDJSON ऑडिट प्रश्न भेजता है और सीढ़ी लाइव स्ट्रीम करता है।',
	auditClear: 'साफ़ करें',
	auditEmpty: 'अभी कोई ऑडिट प्रविष्टि नहीं — स्ट्रीम लाइव निर्णय, बैंड परिवर्तन और टूल कॉल जोड़ती है।',

	bandTitle: 'सक्रिय आंदोलन अनुकूलन',
	bandSub: 'स्थिति → हिस्टेरेसिस → नीति फ्लोर → ट्विन शैडो-मूल्यांकन (फेल-क्लोज़़ड) → ऑडिटेड परिसिस्टेंस',
	bandHistory: 'बैंड इतिहास',
	bandSourceLog: 'situation_log',
	bandSourceStatus: 'एडेप्टिव स्थिति',
	bandAccumulated: 'क्लाइंट-संचित',
	bandShadowEval: 'शैडो मूल्यांकन',
	bandFloorApplied: 'फ़्लोर लागू',
	bandFailClosed: 'फ़ेल-क्लोज़्ड',

	twinTitle: 'ट्विन शैडो-मूल्यांकन तुलना',
	twinSub: 'बेसलाइन बनाम प्रत्याशी क्लोज़र नीति, सीडेड कॉरिडोर ट्विन पर',
	twinRun: 'तुलना चलाएँ',
	twinRunning: 'ट्विन चल रहा है…',
	twinBaseline: 'बेसलाइन',
	twinCandidate: 'प्रत्याशी',
	twinAdvance: 'पूर्व क्लोज़र (सेकंड)',
	twinClosure: 'क्लोज़र',
	twinDisruption: 'बाधा (वाहन·घंटा)',
	twinQueue: 'अधिकतम कतार (मी)',
	twinClosureDur: 'क्लोज़र अवधि (सेकंड)',
	twinTransit: 'प्राथमिकता सफ़र (सेकंड)',
	twinRecovery: 'पुनर्प्राप्ति (सेकंड)',
	twinAmbulance: 'एम्बुलेंस विलंब (सेकंड)',
	twinDelta: 'Δ',
	twinSourceCompare: '/simulate/twin/compare से',
	twinSourceDirect: 'कंपेयर एंडपॉइंट अनुपलब्ध — दो स्पष्ट /simulate/twin बाहें',
	twinCached: 'कैश्ड — माँग पर रिफ़्रेश',
	twinReplications: (n) => `${n} प्रतिकृतियाँ`,

	emuTitle: 'इवेंट एम्युलेटर',
	emuSub: 'लूपबैक :8090 पर कॉज़-टेप इंजेक्शन',
	emuOnline: 'एम्युलेटर ऑनलाइन',
	emuOffline: 'एम्युलेटर ऑफ़लाइन',
	emuCheck: 'स्वास्थ्य जाँच',
	emuLaunch: 'यूज़-केस तैयार करें',
	emuLaunchNote: 'लॉन्च अनुबंध एम्युलेटर स्ट्रीम के पास है; त्रुटियाँ जस की तस दिखती हैं।',

	brand: 'कॉरिडोर अलर्ट · अहमदाबाद',
	tagline: 'बैरिकेड लगने से पहले ही जानें।',
	updated: 'सूचनाएँ पिछली बार जाँची गईं',
	statusActive: 'कॉरिडोर घटना चल रही है',
	statusActiveSub: 'हर हिस्से पर ~२–३ मिनट के छोटे रोक — पूरी सड़क बंद नहीं।',
	statusClear: 'इस समय कोई सड़क प्रतिबंध नहीं',
	statusClearSub: 'ये सूचनाएँ केवल नियोजित कॉरिडोर ठहराव बताती हैं — सामान्य यातायात नहीं।',
	statusDone: 'घटना पूरी — कॉरिडोर खुल गया',
	statusDoneSub: 'यातायात सामान्य हो रहा है।',
	startDemo: 'डेमो काफ़िला चलाएँ',
	freezeTitle: 'कहाँ रोक संभावित है',
	freezeZone: 'सड़क हिस्सा',
	freezeWindow: 'रोक का समय',
	freezeMinutes: (m) => `~${m} मिनट`,
	alternate: 'इसके बदले चलें',
	holdNow: 'अभी रुका हुआ',
	passed: 'खुल गया',
	why: 'इतना छोटा रोक क्यों?',
	whyBody:
		'रोक घटना की लाइव स्थिति (६०–१८० सेकंड पहले) और ट्रैफ़िक पूर्वानुमान से तय होती है — सड़क सिर्फ़ तब बंद रहती है जब काफ़िला सच में पास हो।',
	subscribeTitle: 'अपने फ़ोन पर अलर्ट पाएँ',
	subscribeSub: 'SMS / WhatsApp, नःशुल्क, आपकी भाषा में।',
	phoneLabel: 'मोबाइल नंबर',
	phonePlaceholder: '१० अंकों का नंबर',
	languageLabel: 'अलर्ट की भाषा',
	subscribeBtn: 'सब्सक्राइब करें',
	demoBadge: 'डेमो पूर्वावलोकन — कोई असली SMS/WhatsApp नहीं भेजा जाता',
	previewTitle: 'आपको मिलेगा:',
	subscribed: 'सब्सक्राइब हुआ (डेमो)',
	emergencyTitle: 'एम्बुलेंस या फ़ायर वाहन चला रहे हैं?',
	emergencyBody: 'चालू घटना से ग्रीन-कॉरिडोर समय माँगें — कंट्रोल रूम सेकंडों में जवाब देता है।',
	emergencyCta: 'ग्रीन कॉरिडोर का अनुरोध',
	backToOps: 'ऑपरेटर कंसोल',
	footerNote: 'कॉरिडोर-घटना ऑर्केस्ट्रेशन प्रोटोटाइप। वैकल्पिक मार्ग सांकेतिक हैं।',
	driverTitle: 'ग्रीन कॉरिडोर — 108 ड्राइवर',
	driverSub: 'एक टैप। कंट्रोल रूम सेकंडों में जवाब देता है।',
	driverBack: 'नागरिक अलर्ट',
	noMovement: 'कोई कॉरिडोर सक्रिय नहीं',
	noMovementSub: 'अभी कोई कॉरिडोर घटना नहीं — सामान्य चलें। यह फ़्लो आज़माने के लिए ऊपर से डेमो घटना चुनें।',
	atStretch: 'आप यहाँ हैं',
	urgency: 'मरीज़ की तात्कालिकता',
	urgCardiac: 'हृदय',
	urgTrauma: 'दुर्घटना',
	urgStroke: 'स्ट्रोक',
	urgOther: 'अन्य आपात',
	requestBtn: 'कॉरिडोर समय का अनुरोध',
	evaluating: 'कॉरिडोर गैप आँका जा रहा है…',
	proposalTitle: 'प्लेटफ़ॉर्म का प्रस्तावित समय',
	convoyReachesIn: 'घटना आपके हिस्से तक पहुँचेगी',
	crossBefore: 'इससे पहले पार करें',
	officerLabel: 'ड्यूटी अधिकारी (डेमो)',
	approveBtn: 'मंज़ूर · विंडो खोलें',
	deferBtn: 'टालें',
	windowOpen: 'विंडो खुली — अभी पार करें',
	windowClosesIn: 'विंडो बंद होगी',
	minutesReturned: 'मिनट गोल्डन आवर को लौटाए गए',
	holdPosition: 'रुकें — अगली विंडो',
	baselineNote: 'अनुरोध का कोई रास्ता न हो तो: दर्ज आँकड़ों में एम्बुलेंस ५–२० मिनट रुकती है।',
	openDriverView: '108 ड्राइवर व्यू खोलें',
	demoNote: 'स्क्रिप्टेड सिमुलेशन पर चलने वाला डेमो — कोई असली डिस्पैच नहीं।',
};

const gu: Pack = {
	kind_protected_movement: 'સુરક્ષિત વાહન કાફલો',
	kind_procession: 'રથ યાત્રા જુલૂસ',
	kind_mass_gathering: 'વિશાળ સમાગમ (કુંભ-સ્તર)',
	kind_race: 'રસ્તા દોડ (મેરેથોન)',
	kind_ceremonial: 'ઔપચારિક પરેડ',
	kind_egress_surge: 'સ્ટેડિયમ નિકાસ ભીડ',
	kind_emergency_evacuation: 'પૂર રાહત / ખાલી કરવાનો કાફલો',
	kind_work_zone: 'મૂળભૂત સુવિધા કામગીરી',
	kind_vehicle_pursuit: 'વાહન પર્સ્યુટ',
	kind_crime_incident: 'ગુનાહી ઘટના',
	kind_unknown: 'કોરિડોર ઘટના',

	crowd_LOW: 'ઓછી ભીડ',
	crowd_MODERATE: 'મધ્યમ ભીડ',
	crowd_HIGH: 'ભારે ભીડ',
	crowd_EXTREME: 'અત્યંત ભારે ભીડ',

	band_GREEN: 'GREEN · સામાન્ય',
	band_AMBER: 'AMBER · સાવધ',
	band_RED: 'RED · દરમિયાનગીરી',
	band_BLACK: 'BLACK · નિયંત્રણ લો',

	opsTitle: 'કોરિડોર ઓપરેશન બોર્ડ',
	opsSub: 'એક સાથે ચાલતી કોરિડોર ઘટનાઓ · લાઇવ નિર્ણય ઑડિટ · અનુકૂલનક્ષમ સ્થિતિ બેન્ડ',
	opsSourceLive: 'લાઇવ',
	opsSourceMovements: 'મૂવમેન્ટ્સ API',
	opsSourceOffline: 'ઑફલાઇન પ્રીવ્યૂ',
	opsNoEvents: 'કોઈ સક્રિય કોરિડોર ઘટના નથી',
	opsEventsApiMissing:
		'ઇવેન્ટ્સ API હજી તૈનાત નથી — મૂવમેન્ટ્સ ફોલબેક + ચિહ્નિત ઑફલાઇન પ્રીવ્યૂ દેખાય છે',
	opsPhase: 'તબક્કો',
	opsWindow: 'સમય-વિન્ડો',
	opsRouteSpine: 'માર્ગ સ્તંભ',
	opsSelectEvent: 'પસંદ કરો',
	opsSelected: 'પસંદ કરેલ',
	opsLastUpdated: (ago) => `${ago} સેકંડ પહેલાં અપડેટ`,
	opsStale: 'જૂનું',
	opsNever: 'પહેલા પ્રતિસાદની રાહ',

	auditTitle: 'નિર્ણય-ઑડિટ સ્ટ્રીમ',
	auditSub: 'નિર્ણયક · જેમ્મા એજન્ટ · MCP ટૂલ કોલ, બ્રેનના ઑડિટ ચેનલોમાંથી',
	auditLadder: (rung) => `ચેનલ: ${rung}`,
	auditRungWsEvent: 'WS /ws/event',
	auditRungWsConvoy: 'WS /ws/convoy',
	auditRungPoll: '2 સેકંડ પોલ',
	auditRungUnavailable: 'અનુપલબ્ધ',
	auditProbe: 'એજન્ટને ચકાસો',
	auditProbeHint: '/chat/stream પર એક NDJSON ઑડિટ પ્રશ્ન મોકલે છે અને લેડર લાઇવ સ્ટ્રીમ કરે છે.',
	auditClear: 'સાફ કરો',
	auditEmpty: 'હજી કોઈ ઑડિટ પ્રવેશ નથી — સ્ટ્રીમ લાઇવ નિર્ણયો, બેન્ડ ફેરફારો અને ટૂલ કોલ ઉમેરે છે.',

	bandTitle: 'સક્રિય ચળવળ અનુકૂલન',
	bandSub: 'સ્થિતિ → હિસ્તેરેસિસ → પોલિસી ફ્લોર → ટ્વિન શેડો-એવલ (ફેલ-ક્લોઝ્ડ) → ઑડિટેડ પરિસિસ્ટન્સ',
	bandHistory: 'બેન્ડ ઇતિહાસ',
	bandSourceLog: 'situation_log',
	bandSourceStatus: 'એડેપ્ટિવ સ્થિતિ',
	bandAccumulated: 'ક્લાયન્ટ-સંચિત',
	bandShadowEval: 'શેડો મૂલ્યાંકન',
	bandFloorApplied: 'ફ્લોર લાગુ',
	bandFailClosed: 'ફેલ-ક્લોઝ્ડ',

	twinTitle: 'ટ્વિન શેડો-મૂલ્યાંકન સરખામણી',
	twinSub: 'બેઝલાઇન વિરુદ્ધ ઉમેદવાર ક્લોઝર પોલિસી, સીડેડ કોરિડોર ટ્વિન પર',
	twinRun: 'સરખામણી ચલાવો',
	twinRunning: 'ટ્વિન ચાલુ છે…',
	twinBaseline: 'બેઝલાઇન',
	twinCandidate: 'ઉમેદવાર',
	twinAdvance: 'અગાઉનું ક્લોઝર (સેકંડ)',
	twinClosure: 'ક્લોઝર',
	twinDisruption: 'અવરોધ (વાહન·કલાક)',
	twinQueue: 'મહત્તમ કતાર (મી)',
	twinClosureDur: 'ક્લોઝર સમયગાળો (સેકંડ)',
	twinTransit: 'પ્રાથમિકતા સફર (સેકંડ)',
	twinRecovery: 'પુનઃપ્રાપ્તિ (સેકંડ)',
	twinAmbulance: 'એમ્બ્યુલન્સ વિલંબ (સેકંડ)',
	twinDelta: 'Δ',
	twinSourceCompare: '/simulate/twin/compare મારફતે',
	twinSourceDirect: 'કમ્પેર એન્ડપોઇન્ટ અનુપલબ્ધ — બે સ્પષ્ટ /simulate/twin બાહો',
	twinCached: 'કેશ્ડ — માંગ પર રિફ્રેશ',
	twinReplications: (n) => `${n} પ્રતિકૃતિઓ`,

	emuTitle: 'ઇવેન્ટ એમ્યુલેટર',
	emuSub: 'લૂપબેક :8090 પર કોઝ-ટેપ ઇન્જેક્શન',
	emuOnline: 'એમ્યુલેટર ઑનલાઇન',
	emuOffline: 'એમ્યુલેટર ઑફલાઇન',
	emuCheck: 'હેલ્થ ચકાસો',
	emuLaunch: 'યુઝ-કેસ તૈયાર કરો',
	emuLaunchNote: 'લોન્ચ કોન્ટ્રાક્ટ એમ્યુલેટર સ્ટ્રીમ પાસે છે; ભૂલો જેમ છે તેમ દેખાય છે.',

	brand: 'કોરિડોર એલર્ટ · અમદાવાદ',
	tagline: 'અવરોધક ઊભો થાય તે પહેલાં જાણો.',
	updated: 'સૂચનાઓ છેલ્લે ચકાસી',
	statusActive: 'કોરિડોર ઘટના ચાલુ છે',
	statusActiveSub: 'દરેક ટુકડા પર ~૨–૩ મિનિટના ટૂંકા અવરોધ — આખો રસ્તો બંધ નહીં.',
	statusClear: 'અત્યારે કોઈ રસ્તા પ્રતિબંધ નથી',
	statusClearSub: 'આ સૂચનાઓ ફક્ત આયોજિત કોરિડોર અવરોધ જણાવે છે — સામાન્ય ટ્રાફિક નહીં.',
	statusDone: 'ઘટના પૂરી — કોરિડોર ખુલ્લો',
	statusDoneSub: 'ટ્રાફિક સામાન્ય થઈ રહ્યો છે.',
	startDemo: 'ડેમો કાફલો ચલાવો',
	freezeTitle: 'ક્યાં અવરોધ શક્ય છે',
	freezeZone: 'રસ્તાનો ટુકડો',
	freezeWindow: 'અવરોધનો સમય',
	freezeMinutes: (m) => `~${m} મિનિટ`,
	alternate: 'આ બદલે ચાલો',
	holdNow: 'અત્યારે બંધ',
	passed: 'ખુલ્લો',
	why: 'આટલો ટૂંકો અવરોધ કેમ?',
	whyBody:
		'અવરોધ ઘટનાની લાઇવ સ્થિતિ (૬૦–૧૮૦ સેકંડ પહેલાં) અને ટ્રાફિક આગાહી પરથી નક્કી થાય છે — રસ્તો માત્ર ત્યારે જ બંધ રહે છે જ્યારે કાફલો સાચે જ પાસે હોય.',
	subscribeTitle: 'તમારા ફોન પર એલર્ટ મેળવો',
	subscribeSub: 'SMS / WhatsApp, મફત, તમારી ભાષામાં.',
	phoneLabel: 'મોબાઇલ નંબર',
	phonePlaceholder: '૧૦ આંકડાનો નંબર',
	languageLabel: 'એલર્ટની ભાષા',
	subscribeBtn: 'સબ્સ્ક્રાઇબ કરો',
	demoBadge: 'ડેમો પ્રીવ્યૂ — કોઈ અસલી SMS/WhatsApp મોકલાતો નથી',
	previewTitle: 'તમને આ મળે:',
	subscribed: 'સબ્સ્ક્રાઇબ થયું (ડેમો)',
	emergencyTitle: 'એમ્બ્યુલન્સ કે ફાયર વાહન ચલાવો છો?',
	emergencyBody: 'ચાલુ ઘટના માટે ગ્રીન-કોરિડોર સમય માંગો — કંટ્રોલ રૂમ સેકંડોમાં જવાબ આપે છે.',
	emergencyCta: 'ગ્રીન કોરિડોર વિનંતી',
	backToOps: 'ઓપરેટર કન્સોલ',
	footerNote: 'કોરિડોર-ઘટના ઓર્કેસ્ટ્રેશન પ્રોટોટાઇપ. વૈકલ્પિક માર્ગો સૂચક છે.',
	driverTitle: 'ગ્રીન કોરિડોર — 108 ડ્રાઇવર',
	driverSub: 'એક ટેપ. કંટ્રોલ રૂમ સેકંડોમાં જવાબ આપે છે.',
	driverBack: 'નાગરિક એલર્ટ',
	noMovement: 'કોઈ કોરિડોર સક્રિય નથી',
	noMovementSub: 'અત્યારે કોઈ કોરિડોર ઘટના નથી — સામાન્ય ચલાવો. આ ફ્લો અજમાવવા ઉપરથી ડેમો ઘટના પસંદ કરો.',
	atStretch: 'તમે અહીં છો',
	urgency: 'દર્દીની તાત્કાલિકતા',
	urgCardiac: 'હૃદય',
	urgTrauma: 'અકસ્માત',
	urgStroke: 'સ્ટ્રોક',
	urgOther: 'અન્ય કટોકટી',
	requestBtn: 'કોરિડોર સમયની વિનંતી',
	evaluating: 'કોરિડોર ગેપ મૂલવાઈ રહી છે…',
	proposalTitle: 'પ્લેટફોર્મનો સૂચવેલો સમય',
	convoyReachesIn: 'ઘટના તમારા ટુકડા પર પહોંચશે',
	crossBefore: 'આ પહેલાં પસાર કરો',
	officerLabel: 'ડ્યુટી ઓફિસર (ડેમો)',
	approveBtn: 'મંજૂર · વિન્ડો ખોલો',
	deferBtn: 'ટાળો',
	windowOpen: 'વિન્ડો ખુલ્લી — હમણાં પસાર કરો',
	windowClosesIn: 'વિન્ડો બંધ થશે',
	minutesReturned: 'મિનિટ ગોલ્ડન આવરને પરત',
	holdPosition: 'રોકાઓ — આગામી વિન્ડો',
	baselineNote: 'વિનંતીનો કોઈ રસ્તો ન હોય તો: નોંધાયેલા આંકડા મુજબ એમ્બ્યુલન્સ ૫–૨૦ મિનિટ રોકાય છે.',
	openDriverView: '108 ડ્રાઇવર વ્યૂ ખોલો',
	demoNote: 'સ્ક્રિપ્ટેડ સિમ્યુલેશન પર ચાલતો ડેમો — કોઈ અસલી ડિસ્પેચ નથી.',
};

const PACKS: Record<string, Pack> = { en, hi, gu };

export function t(lang: string, key: string, ...args: unknown[]): string {
	const pack = PACKS[lang] ?? PACKS.en;
	const val = pack[key] ?? PACKS.en[key];
	if (typeof val === 'function') return String(val(...args));
	return val != null ? String(val) : key;
}

/**
 * Event-kind label with a fallback: the per-language kind name, else the
 * generic 'Corridor event' wording — NEVER the raw `kind_*` key, whatever
 * kind string the API sends (new archetypes degrade to the generic label).
 */
export function kindLabel(lang: string, kind: string | null | undefined): string {
	const key = `kind_${kind ?? ''}`;
	const val = t(lang, key);
	return val === key ? t(lang, 'kind_unknown') : val;
}

/** Indic-script digits for the demo alert preview (Indian SMS gateways). */
const DIGITS: Record<string, string> = {
	en: '0123456789',
	hi: '०१२३४५६७८९',
	gu: '૦૧૨૩૪૫૬૭૮૯',
};

export function localizeNumber(n: number | string, lang: string): string {
	const table = DIGITS[lang] ?? DIGITS.en;
	return String(n).replace(/[0-9]/g, (d) => table[+d]);
}

/**
 * Additive pack registration (design-shell integration point).
 *
 * The core packs above are FROZEN — existing keys are never overridden.
 * Streams register their own strings per language (en+hi+gu mandatory for
 * every new user-facing string); t() keeps its fallback chain
 * (lang → en → raw key) so a partially-registered language degrades to
 * English rather than key text.
 */
export function registerPack(code: string, pack: Pack): void {
	const existing = PACKS[code] ?? (PACKS[code] = {});
	for (const [key, value] of Object.entries(pack)) {
		if (!(key in existing)) existing[key] = value;
	}
}
