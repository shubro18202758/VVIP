/**
 * Ops-screens string pack (EN / HI / GU) — operator-surface copy for the
 * situation wall, decision queue, dispatch tiles, signal gate card,
 * watchlist console and pursuit plan.
 *
 * The i18n core (strings.ts) stays frozen; this module carries ONLY the new
 * operator vocabulary and exposes `tOps()` which consults this pack first
 * and falls back to the core `t()` — so a missing key here still renders the
 * core's copy or the raw key (never a crash), and every user-facing string
 * this stream adds ships in all three languages.
 */

import { registerPack, t } from './strings';
import type { LangCode } from './strings';

/** Re-export the frozen core resolver so ops components import one module. */
export { t };

type Pack = Record<string, string | ((...args: unknown[]) => string)>;

const en: Pack = {
	// ── Event kinds (the watchlist-anpr additions the live oracle serves) ──
	kind_vehicle_pursuit: 'Vehicle pursuit',
	kind_crime_incident: 'Crime incident',

	// ── Situation masthead ─────────────────────────────────────────────
	mastheadPosture: 'city posture',
	mastheadDwell: (band, tick, need) => `${band} held · tick ${tick}/${need}`,
	mastheadWipeIn: 'committing band',
	mastheadOfficer: 'Duty officer',
	mastheadOfficerPlaceholder: 'name on console',
	mastheadOfficerSet: 'on console',
	mastheadOfficerMissing: 'set a named officer to enable commit actions',
	posture_GREEN: 'Normal operations — no corridor interventions pending.',
	posture_AMBER: 'Corridor operations active — watch the queue and the dwell before any band move.',
	posture_RED: 'Intervene — multiple concurrent pressures, decisions pending beyond tolerance.',
	posture_BLACK: 'Take over — citywide coordination under the control room.',

	// ── Commander counters ─────────────────────────────────────────────
	counterActiveEvents: 'active events',
	counterPendingDecisions: 'pending human decisions',
	counterUnitsDispatched: 'units dispatched',
	counterAmbulanceWindows: 'ambulance windows granted',

	// ── Decision queue ─────────────────────────────────────────────────
	queueTitle: 'Decision Queue',
	queueSub: 'machines propose · a named officer disposes',
	queueEmpty: 'No pending recommendations — the queue is honest about being empty.',
	queueEchallan: 'e-challan recommendation',
	queueEvidence: 'evidence',
	queueHideEvidence: 'hide evidence',
	queueRule: 'rule',
	queueRetention: 'retention',
	queueApprove: 'Approve',
	queueReject: 'Reject with reason',
	queueReasonPlaceholder: 'reason (logged to the audit trail)',
	queueDecided: (status) => `decided · ${status}`,
	queueGoldenHour: 'golden hour',
	queueOpenPursuit: 'Open pursuit',
	queuePursuitConfirm: 'Confirm pursuit opening — a human initiates, never the machine.',

	// ── Dispatch tiles (Jan Rakshak) ───────────────────────────────────
	dispatchTitle: 'Jan Rakshak Dispatch',
	dispatchSub: 'requested → approved → en route → on scene',
	dispatchRequestUnit: 'Request unit',
	dispatchUnitPlaceholder: 'unit id (e.g. JR-EM-01)',
	dispatchApprove: 'Approve',
	dispatchDefer: 'Defer',
	dispatchEnRoute: 'en route',
	dispatchOnScene: 'on scene',
	dispatchComplete: 'complete',
	dispatchCancel: 'cancel',
	dispatchAudit: 'audit trail',
	dispatchNone: 'No dispatches yet.',
	dispatchAtWaypoint: (junction) => `waypoint junction ${junction}`,

	// ── Signal gate card ───────────────────────────────────────────────
	gateTitle: 'City Signal Plans',
	gateSub: 'the gated writer — proposed vs held, in full view',
	gateWriterEnabled: 'writer ENABLED',
	gateWriterDisabled: 'writer DISABLED',
	gateCityBand: 'city band',
	gateSubjects: (n) => `${n} band subjects`,
	gatePlanTableBand: 'band',
	gatePlanTableShare: 'priority share',
	gatePlanTableFloor: 'cross green floor (s)',
	gatePlanTableWrites: 'junction writes',
	gateApply: 'APPLY plan',
	gateApplying: 'applying…',
	gateApplied: 'plan applied',
	gateHeldBaseline: 'held at baseline',
	gateRefusal: 'refusal',
	gateLastPlan: 'last plan',
	gateRestore: 'RESTORE',
	gateHistory: 'history',
	gateHistoryEmpty: 'no history rows yet',
	gateScopeNote: 'scope: city (all programmed junctions)',

	// ── AMA module ─────────────────────────────────────────────────────
	amaStages: 'stages',
	amaFloors: 'protocol floors',
	amaWriterPosture: 'writer posture',
	amaShadowEval: 'shadow eval',

	// ── Watchlist console ──────────────────────────────────────────────
	watchTitle: 'Pursuit & Watchlist',
	watchSub: 'earmarked plates · ANPR sightings · live traces',
	watchPlate: 'plate',
	watchState: 'earmark',
	watchBasis: 'lawful basis',
	watchRetention: 'retention until',
	watchLastSeen: 'last seen',
	watchEarmark: 'earmark',
	watchClear: 'clear',
	watchReasonPlaceholder: 'reason (min 3 chars)',
	watchDays: 'days',
	watchNone: 'No earmarked vehicles.',
	watchSpeed: (kmh) => `${kmh} km/h`,
	watchSource: 'source',

	// ── Pursuit plan ───────────────────────────────────────────────────
	pursuitTitle: 'Interception Choke Plan',
	pursuitRank: 'choke',
	pursuitEta: 'graph cost (s)',
	pursuitHold: 'hold cost',
	pursuitSignalized: 'signalized',
	pursuitCaption: 'assists the operator — it never dispatches on its own',
	pursuitNoneForEvent: 'not a pursuit event — no interception plan',
	pursuitLatestSighting: 'latest sighting',
	pursuitSeedSegment: 'seed segment',

	// ── Event dossier ──────────────────────────────────────────────────
	dossierTitle: 'Event Dossier',
	dossierMetadata: 'metadata',
	dossierPhases: 'phases',
	dossierAuditNarrative: 'audit narrative',
	dossierAdvisory: 'advisory preview',
	dossierWindow: 'window',

	// ── Degradation ledger ─────────────────────────────────────────────
	ledgerTitle: 'Degradation Ledger',
	ledgerSub: 'recurring failures mined from the decisions_log audit trail',
	ledgerEmpty: 'No degradation on record — the audit log is honest about being empty.',
	ledgerEmptyHint: 'every checkpoint answered through its intended tier; nothing withheld, nothing fell back',
	ledgerTimes: (n) => `${n}×`,
	ledgerFirstSeen: (t) => `first ${t}`,
	ledgerSampleRef: 'sample',
	ledgerScanLine: (rows, contexts) => `${rows} audit rows across ${contexts} movements`,
	ledgerTruncated: 'scan truncated — more movements on record than the ledger reads',

	// ── Common ─────────────────────────────────────────────────────────
	updatedAgo: (s) => `updated ${s}s ago`,
	labelLive: 'live',
	labelEmulated: 'EMULATED',
	labelOffline: 'offline',
	labelNotDeployed: 'not deployed',
	labelStale: 'stale',
	gateRefusedNoOfficer: 'refused — no named duty officer on console',
	canvasLockTitle: 'camera locked to subject',
	canvasUnlock: 'release lock',
	tourTitle: 'Guided tour',
	tourStart: 'Start tour',
	tourStop: 'Stop tour',
	tourSteps: 'tour step',
};

const hi: Pack = {
	// ── घटना प्रकार (लाइव ऑरेकल की वॉचलिस्ट किस्में) ────────────────────
	kind_vehicle_pursuit: 'वाहन पर्सुट',
	kind_crime_incident: 'अपराध घटना',

	// ── स्थिति मास्टहेड ────────────────────────────────────────────────
	mastheadPosture: 'शहर की स्थिति',
	mastheadDwell: (band, tick, need) => `${band} रोका हुआ · टिक ${tick}/${need}`,
	mastheadWipeIn: 'बैंड लागू हो रहा है',
	mastheadOfficer: 'ड्यूटी अधिकारी',
	mastheadOfficerPlaceholder: 'कंसोल पर नाम',
	mastheadOfficerSet: 'कंसोल पर हैं',
	mastheadOfficerMissing: 'स्वीकृति-क्रियाएँ सक्रिय करने के लिए अधिकारी का नाम दें',
	posture_GREEN: 'सामान्य संचालन — कोई गलियारा हस्तक्षेप लंबित नहीं।',
	posture_AMBER: 'गलियारा संचालन सक्रिय — कतार और निर्णय-प्रतीक्षा देखें।',
	posture_RED: 'हस्तक्षेप करें — कई समवर्ती दबाव, निर्णय सहनशीलता से पार।',
	posture_BLACK: 'नियंत्रण कक्ष के अधीन नगर-व्यापी समन्वय।',

	// ── कमांडर काउंटर ─────────────────────────────────────────────────
	counterActiveEvents: 'सक्रिय घटनाएँ',
	counterPendingDecisions: 'लंबित मानवीय निर्णय',
	counterUnitsDispatched: 'तैनात यूनिट',
	counterAmbulanceWindows: 'स्वीकृत एम्बुलेंस विंडो',

	// ── निर्णय कतार ────────────────────────────────────────────────────
	queueTitle: 'निर्णय कतार',
	queueSub: 'मशीन सुझाती है · नामित अधिकारी निर्णय लेता है',
	queueEmpty: 'कोई लंबित सिफ़ारिश नहीं — कतार खाली होना ईमानदारी से दिखाई दे रही है।',
	queueEchallan: 'ई-चालान सिफ़ारिश',
	queueEvidence: 'साक्ष्य',
	queueHideEvidence: 'साक्ष्य छिपाएँ',
	queueRule: 'नियम',
	queueRetention: 'प्रतिधारण',
	queueApprove: 'स्वीकृत करें',
	queueReject: 'कारण सहित अस्वीकारें',
	queueReasonPlaceholder: 'कारण (ऑडिट में दर्ज होगा)',
	queueDecided: (status) => `निर्णीत · ${status}`,
	queueGoldenHour: 'गोल्डन आवर',
	queueOpenPursuit: 'पर्सुट शुरू करें',
	queuePursuitConfirm: 'पर्सुट खोलने की पुष्टि करें — मनुष्य ही शुरू करता है, मशीन कभी नहीं।',

	// ── डिस्पैच टाइल (जन रक्षक) ────────────────────────────────────────
	dispatchTitle: 'जन रक्षक डिस्पैच',
	dispatchSub: 'अनुरोधित → स्वीकृत → रास्ते में → स्थल पर',
	dispatchRequestUnit: 'यूनिट माँगें',
	dispatchUnitPlaceholder: 'यूनिट आईडी (जैसे JR-EM-01)',
	dispatchApprove: 'स्वीकृत करें',
	dispatchDefer: 'टालें',
	dispatchEnRoute: 'रास्ते में',
	dispatchOnScene: 'स्थल पर',
	dispatchComplete: 'पूर्ण',
	dispatchCancel: 'रद्द करें',
	dispatchAudit: 'ऑडिट रिकॉर्ड',
	dispatchNone: 'अभी कोई डिस्पैच नहीं।',
	dispatchAtWaypoint: (junction) => `वेपॉइंट जंक्शन ${junction}`,

	// ── सिग्नल गेट कार्ड ───────────────────────────────────────────────
	gateTitle: 'नगर सिग्नल योजनाएँ',
	gateSub: 'गेटेड राइटर — प्रस्तावित बनाम रोकी हुई, पूरे दृश्य में',
	gateWriterEnabled: 'राइटर सक्रिय',
	gateWriterDisabled: 'राइटर निष्क्रिय',
	gateCityBand: 'नगर बैंड',
	gateSubjects: (n) => `${n} बैंड विषय`,
	gatePlanTableBand: 'बैंड',
	gatePlanTableShare: 'प्राथमिकता हिस्सा',
	gatePlanTableFloor: 'क्रॉस ग्रीन फ़्लोर (से)',
	gatePlanTableWrites: 'जंक्शन राइट',
	gateApply: 'योजना लागू करें',
	gateApplying: 'लागू हो रही है…',
	gateApplied: 'योजना लागू',
	gateHeldBaseline: 'बेसलाइन पर रोकी गई',
	gateRefusal: 'अस्वीकृति',
	gateLastPlan: 'अंतिम योजना',
	gateRestore: 'पुनर्स्थापित करें',
	gateHistory: 'इतिहास',
	gateHistoryEmpty: 'अभी कोई इतिहास पंक्ति नहीं',
	gateScopeNote: 'दायरा: नगर (सभी प्रोग्राम्ड जंक्शन)',

	// ── AMA मॉड्यूल ─────────────────────────────────────────────────────
	amaStages: 'चरण',
	amaFloors: 'प्रोटोकॉल फ़्लोर',
	amaWriterPosture: 'राइटर स्थिति',
	amaShadowEval: 'शैडो मूल्यांकन',

	// ── वॉचलिस्ट कंसोल ─────────────────────────────────────────────────
	watchTitle: 'पर्सुट और वॉचलिस्ट',
	watchSub: 'चिह्नित नंबर-प्लेट · ANPR दृष्टियाँ · लाइव ट्रेस',
	watchPlate: 'प्लेट',
	watchState: 'इयरमार्क',
	watchBasis: 'वैधानिक आधार',
	watchRetention: 'प्रतिधारण तक',
	watchLastSeen: 'अंतिम दर्शन',
	watchEarmark: 'चिह्नित करें',
	watchClear: 'हटाएँ',
	watchReasonPlaceholder: 'कारण (न्यूनतम 3 अक्षर)',
	watchDays: 'दिन',
	watchNone: 'कोई चिह्नित वाहन नहीं।',
	watchSpeed: (kmh) => `${kmh} किमी/घं`,
	watchSource: 'स्रोत',

	// ── पर्सुट योजना ───────────────────────────────────────────────────
	pursuitTitle: 'अवरोधन चोक योजना',
	pursuitRank: 'चोक',
	pursuitEta: 'ग्राफ़ लागत (से)',
	pursuitHold: 'रोक लागत',
	pursuitSignalized: 'सिग्नलयुक्त',
	pursuitCaption: 'ऑपरेटर की सहायता करता है — स्वयं कभी डिस्पैच नहीं करता',
	pursuitNoneForEvent: 'पर्सुट घटना नहीं — कोई अवरोधन योजना नहीं',
	pursuitLatestSighting: 'अंतिम दृष्टि',
	pursuitSeedSegment: 'सीड सेगमेंट',

	// ── घटना डॉसियर ────────────────────────────────────────────────────
	dossierTitle: 'घटना डॉसियर',
	dossierMetadata: 'मेटाडेटा',
	dossierPhases: 'चरण',
	dossierAuditNarrative: 'ऑडिट विवरण',
	dossierAdvisory: 'सलाह पूर्वावलोकन',
	dossierWindow: 'समय-खिड़की',

	// ── डिग्रेडेशन लेजर ────────────────────────────────────────────────
	ledgerTitle: 'डिग्रेडेशन लेजर',
	ledgerSub: 'decisions_log ऑडिट रिकॉर्ड से खनित आवर्ती विफलताएँ',
	ledgerEmpty: 'कोई डिग्रेडेशन दर्ज नहीं — ऑडिट लॉग खाली होना ईमानदारी से बताता है।',
	ledgerEmptyHint: 'हर चेकपॉइंट अपने निर्धारित टियर से उत्तर दिया; कुछ रोका नहीं गया, कुछ गिरा नहीं',
	ledgerTimes: (n) => `${n}×`,
	ledgerFirstSeen: (t) => `पहला ${t}`,
	ledgerSampleRef: 'नमूना',
	ledgerScanLine: (rows, contexts) => `${contexts} आंदोलनों की ${rows} ऑडिट पंक्तियाँ`,
	ledgerTruncated: 'स्कैन कटा — लेजर जितना पढ़ता है उससे अधिक आंदोलन दर्ज हैं',

	// ── सामान्य ─────────────────────────────────────────────────────────
	updatedAgo: (s) => `${s} से पहले अपडेट`,
	labelLive: 'लाइव',
	labelEmulated: 'EMULATED',
	labelOffline: 'ऑफ़लाइन',
	labelNotDeployed: 'तैनात नहीं',
	labelStale: 'पुराना',
	gateRefusedNoOfficer: 'अस्वीकृत — कंसोल पर कोई नामित ड्यूटी अधिकारी नहीं',
	canvasLockTitle: 'कैमरा विषय पर लॉक',
	canvasUnlock: 'लॉक हटाएँ',
	tourTitle: 'निर्देशित भ्रमण',
	tourStart: 'भ्रमण शुरू करें',
	tourStop: 'भ्रमण रोकें',
	tourSteps: 'भ्रमण चरण',
};

const gu: Pack = {
	// ── ઘટના પ્રકાર (લાઇવ ઓરેકલની વોચલિસ્ટ કિસ્મો) ──────────────────────────
	kind_vehicle_pursuit: 'વાહન પર્સ્યુટ',
	kind_crime_incident: 'ગુનાહી ઘટના',

	// ── સ્થિતિ માસ્ટહેડ ────────────────────────────────────────────────
	mastheadPosture: 'શહેરની સ્થિતિ',
	mastheadDwell: (band, tick, need) => `${band} રોકાયેલું · ટિક ${tick}/${need}`,
	mastheadWipeIn: 'બેન્ડ લાગુ થાય છે',
	mastheadOfficer: 'ડ્યુટી ઓફિસર',
	mastheadOfficerPlaceholder: 'કન્સોલ પર નામ',
	mastheadOfficerSet: 'કન્સોલ પર છે',
	mastheadOfficerMissing: 'મંજૂરી-ક્રિયાઓ ચાલુ કરવા ઓફિસરનું નામ આપો',
	posture_GREEN: 'સામાન્ય સંચાલન — કોઈ કોરિડોર દખલ બાકી નથી.',
	posture_AMBER: 'કોરિડોર સંચાલન સક્રિય — કતાર અને નિર્ણય-રાહ જુઓ.',
	posture_RED: 'દખલ કરો — ઘણા સાથે દબાણો, નિર્ણયો સહનશક્તિ પાર.',
	posture_BLACK: 'કંટ્રોલ રૂમ હેઠળ શહેરવ્યાપી સંકલન.',

	// ── કમાન્ડર કાઉન્ટર ───────────────────────────────────────────────
	counterActiveEvents: 'સક્રિય ઘટનાઓ',
	counterPendingDecisions: 'બાકી માનવીય નિર્ણયો',
	counterUnitsDispatched: 'તૈનાત યુનિટ',
	counterAmbulanceWindows: 'મંજૂર એમ્બ્યુલન્સ વિન્ડો',

	// ── નિર્ણય કતાર ────────────────────────────────────────────────────
	queueTitle: 'નિર્ણય કતાર',
	queueSub: 'મશીન સૂચે છે · નામાંકિત ઓફિસર નક્કી કરે છે',
	queueEmpty: 'કોઈ બાકી ભલામણ નથી — ખાલી કતાર પ્રામાણિકપણે દેખાય છે.',
	queueEchallan: 'ઈ-ચલાન ભલામણ',
	queueEvidence: 'પુરાવો',
	queueHideEvidence: 'પુરાવો છુપાવો',
	queueRule: 'નિયમ',
	queueRetention: 'જાળવણી',
	queueApprove: 'મંજૂર કરો',
	queueReject: 'કારણ સાથે નકારો',
	queueReasonPlaceholder: 'કારણ (ઑડિટમાં નોંધાશે)',
	queueDecided: (status) => `નિર્ણિત · ${status}`,
	queueGoldenHour: 'ગોલ્ડન આવર',
	queueOpenPursuit: 'પર્સ્યુટ શરૂ કરો',
	queuePursuitConfirm: 'પર્સ્યુટ ખોલવાની પુષ્ટિ કરો — માણસ જ શરૂ કરે છે, મશીન કદી નહીં.',

	// ── ડિસ્પેચ ટાઇલ (જન રક્ષક) ────────────────────────────────────────
	dispatchTitle: 'જન રક્ષક ડિસ્પેચ',
	dispatchSub: 'વિનંતી → મંજૂર → રસ્તામાં → સ્થળ પર',
	dispatchRequestUnit: 'યુનિટ માંગો',
	dispatchUnitPlaceholder: 'યુનિટ આઈડી (જેમ કે JR-EM-01)',
	dispatchApprove: 'મંજૂર કરો',
	dispatchDefer: 'ટાળો',
	dispatchEnRoute: 'રસ્તામાં',
	dispatchOnScene: 'સ્થળ પર',
	dispatchComplete: 'પૂર્ણ',
	dispatchCancel: 'રદ કરો',
	dispatchAudit: 'ઑડિટ રેકોર્ડ',
	dispatchNone: 'હજી કોઈ ડિસ્પેચ નથી.',
	dispatchAtWaypoint: (junction) => `વેપોઇન્ટ જંકશન ${junction}`,

	// ── સિગ્નલ ગેટ કાર્ડ ───────────────────────────────────────────────
	gateTitle: 'શહેર સિગ્નલ યોજનાઓ',
	gateSub: 'ગેટેડ રાઇટર — સૂચવેલ વિ. રોકાયેલ, આખા દૃશ્યમાં',
	gateWriterEnabled: 'રાઇટર સક્રિય',
	gateWriterDisabled: 'રાઇટર નિષ્ક્રિય',
	gateCityBand: 'શહેર બેન્ડ',
	gateSubjects: (n) => `${n} બેન્ડ વિષય`,
	gatePlanTableBand: 'બેન્ડ',
	gatePlanTableShare: 'પ્રાથમિકતા હિસ્સો',
	gatePlanTableFloor: 'ક્રોસ ગ્રીન ફ્લોર (સે)',
	gatePlanTableWrites: 'જંકશન રાઇટ',
	gateApply: 'યોજના લાગુ કરો',
	gateApplying: 'લાગુ થાય છે…',
	gateApplied: 'યોજના લાગુ',
	gateHeldBaseline: 'બેઝલાઇન પર રોકાયેલી',
	gateRefusal: 'નકારણી',
	gateLastPlan: 'છેલ્લી યોજના',
	gateRestore: 'પુનઃસ્થાપિત કરો',
	gateHistory: 'ઇતિહાસ',
	gateHistoryEmpty: 'હજી કોઈ ઇતિહાસ પંક્તિ નથી',
	gateScopeNote: 'વ્યાપ: શહેર (બધા પ્રોગ્રામ્ડ જંકશન)',

	// ── AMA મોડ્યુલ ─────────────────────────────────────────────────────
	amaStages: 'તબક્કા',
	amaFloors: 'પ્રોટોકોલ ફ્લોર',
	amaWriterPosture: 'રાઇટર સ્થિતિ',
	amaShadowEval: 'શેડો મૂલ્યાંકન',

	// ── વોચલિસ્ટ કન્સોલ ─────────────────────────────────────────────────
	watchTitle: 'પર્સ્યુટ અને વોચલિસ્ટ',
	watchSub: 'ચિહ્નિત નંબર-પ્લેટ · ANPR દર્શનો · લાઇવ ટ્રેસ',
	watchPlate: 'પ્લેટ',
	watchState: 'ઇયરમાર્ક',
	watchBasis: 'કાયદાકીય આધાર',
	watchRetention: 'જાળવણી સુધી',
	watchLastSeen: 'છેલ્લો દેખાવ',
	watchEarmark: 'ચિહ્નિત કરો',
	watchClear: 'હટાવો',
	watchReasonPlaceholder: 'કારણ (ઓછામાં ઓછા 3 અક્ષર)',
	watchDays: 'દિવસ',
	watchNone: 'કોઈ ચિહ્નિત વાહન નથી.',
	watchSpeed: (kmh) => `${kmh} કિમી/કલાક`,
	watchSource: 'સ્રોત',

	// ── પર્સ્યુટ યોજના ───────────────────────────────────────────────────
	pursuitTitle: 'અવરોધન ચોક યોજના',
	pursuitRank: 'ચોક',
	pursuitEta: 'ગ્રાફ ખર્ચ (સે)',
	pursuitHold: 'રોક ખર્ચ',
	pursuitSignalized: 'સિગ્નલયુક્ત',
	pursuitCaption: 'ઓપરેટરને મદદ કરે છે — પોતે કદી ડિસ્પેચ કરતું નથી',
	pursuitNoneForEvent: 'પર્સ્યુટ ઘટના નથી — કોઈ અવરોધન યોજના નથી',
	pursuitLatestSighting: 'છેલ્લું દર્શન',
	pursuitSeedSegment: 'સીડ સેગમેન્ટ',

	// ── ઘટના ડોસિયર ──────────────────────────────────────────────────────
	dossierTitle: 'ઘટના ડોસિયર',
	dossierMetadata: 'મેટાડેટા',
	dossierPhases: 'તબક્કા',
	dossierAuditNarrative: 'ઑડિટ વિવરણ',
	dossierAdvisory: 'સલાહ પૂર્વાવલોકન',
	dossierWindow: 'સમય-વિન્ડો',

	// ── ડિગ્રેડેશન લેજર ──────────────────────────────────────────────────
	ledgerTitle: 'ડિગ્રેડેશન લેજર',
	ledgerSub: 'decisions_log ઑડિટ રેકોર્ડમાંથી ખોદેલી પુનરાવર્તિત નિષ્ફળતાઓ',
	ledgerEmpty: 'કોઈ ડિગ્રેડેશન નોંધાયું નથી — ઑડિટ લોગ ખાલી હોવાનું પ્રામાણિકપણે બતાવે છે.',
	ledgerEmptyHint: 'દરેક ચેકપોઇન્ટે પોતાના નિર્ધારિત ટિયરથી જવાબ આપ્યો; કંઈ રોકાયું નહીં, કંઈ પડ્યું નહીં',
	ledgerTimes: (n) => `${n}×`,
	ledgerFirstSeen: (t) => `પ્રથમ ${t}`,
	ledgerSampleRef: 'નમૂનો',
	ledgerScanLine: (rows, contexts) => `${contexts} આંદોલનોની ${rows} ઑડિટ પંક્તિઓ`,
	ledgerTruncated: 'સ્કેન કાપ્યું — લેજર જેટલું વાંચે છે તેથી વધુ આંદોલનો નોંધાયેલા છે',

	// ── સામાન્ય ─────────────────────────────────────────────────────────
	updatedAgo: (s) => `${s} પહેલાં અપડેટ`,
	labelLive: 'લાઇવ',
	labelEmulated: 'EMULATED',
	labelOffline: 'ઑફલાઇન',
	labelNotDeployed: 'તૈનાત નથી',
	labelStale: 'જૂનું',
	gateRefusedNoOfficer: 'નકારાયું — કન્સોલ પર કોઈ નામાંકિત ડ્યુટી ઓફિસર નથી',
	canvasLockTitle: 'કૅમેરા વિષય પર લોક',
	canvasUnlock: 'લોક હટાવો',
	tourTitle: 'માર્ગદર્શિત પ્રવાસ',
	tourStart: 'પ્રવાસ શરૂ કરો',
	tourStop: 'પ્રવાસ રોકો',
	tourSteps: 'પ્રવાસ તબક્કો',
};

const OPS_PACKS: Record<string, Pack> = { en, hi, gu };

// Register into the core (design-shell's sanctioned additive mechanism) so
// primitives that resolve via core t() can read these keys too. registerPack
// never overrides existing keys; importing this module applies the effect.
registerPack('en', en);
registerPack('hi', hi);
registerPack('gu', gu);

/**
 * Trilingual lookup for ops-screens copy: this pack first, then the frozen
 * core pack, then the raw key (the core's documented fallback behaviour).
 */
export function tOps(lang: string, key: string, ...args: unknown[]): string {
	const pack = OPS_PACKS[lang] ?? OPS_PACKS.en;
	const val = pack[key] ?? null;
	if (typeof val === 'function') return String(val(...args));
	if (typeof val === 'string') return val;
	return t(lang as LangCode, key, ...args);
}
