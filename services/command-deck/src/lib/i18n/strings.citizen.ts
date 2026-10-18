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

import { registerPack, t } from './strings';

type Pack = Record<string, string | ((...args: unknown[]) => string)>;

// ─── EN ─────────────────────────────────────────────────────────────────────

const en: Pack = {
	// ── /citizen — "Your road, next 2 hours" ────────────────────────────
	citizen_kicker: 'Your road, next 2 hours',
	citizen_explainer_link: 'Why roads hold',
	strip_calm: 'Roads are moving normally across the city.',
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
		'No live request endpoint is deployed — reach-in and window numbers are demo-scale until one lands.',
	corridor_map: 'Corridor map',
	corridor_granted_stretch: 'Granted stretch',
	unit_en_route: 'Jan Rakshak unit en route',
	unit_on_scene: 'Jan Rakshak unit on scene',
	units_note_fallback: 'Live dispatch feed not deployed — unit states come from the scripted demo.',
	control_room: 'Control room',
	control_room_line: '108 control · stay on channel',
	after_action: (n) => `~${n} minutes saved vs documented 5–20 minute holds`,
	map_note_fallback: 'Showing the corridor spine from event data — live junction states not deployed.',

	// ── /comms — advisory desk (operator) ───────────────────────────────
	comms_title: 'Advisory desk',
	comms_sub: 'One fact, every audience, every language — drafted for the record, never auto-sent.',
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
		'No message gateway is deployed. Advisories are drafted and logged for the audit record — delivery is a future integration, shown honestly.',
	comms_dispatch_threads: 'Dispatch threads',
	comms_dispatch_live: 'live',
	comms_dispatch_demo: 'scripted demo',
	comms_dispatch_none: 'No dispatches recorded for this event yet.',
	comms_all_clear: 'Draft all-clear',
	comms_audit_echo: 'Advisory generation is echoed into the decision audit stream.',

	// Generated advisory templates (args are already-localized strings).
	adv_citizen_hold: (kind, minutes, clock, alt) =>
		`${kind}: ~${minutes} min hold on the route ${clock}. Use ${alt} instead. Roads reopen on time.`,
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
};

// ─── HI ─────────────────────────────────────────────────────────────────────

const hi: Pack = {
	// ── /citizen ────────────────────────────────────────────────────────
	citizen_kicker: 'आपकी सड़क, अगले 2 घंटे',
	citizen_explainer_link: 'सड़कें क्यों रुकती हैं',
	strip_calm: 'पूरे शहर में सड़कें सामान्य चल रही हैं।',
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

	// ── /driver ─────────────────────────────────────────────────────────
	driver_kicker: 'ग्रीन कॉरिडोर',
	golden_hour: 'गोल्डन आवर',
	golden_elapsed: 'बीत चुका',
	phase_idle: 'निष्क्रिय',
	phase_evaluating: 'आँका जा रहा है',
	phase_proposal: 'प्रस्ताव',
	phase_open: 'विंडो खुली',
	phase_deferred: 'टाला गया',
	doctrine_note: 'ग्रीन कॉरिडोर एम्बुलेंस के चौराहे पहुँचने से 60–180 सेकंड पहले खुलता है।',
	scripted_badge: 'स्क्रिप्टेड फ़ॉलबैक',
	scripted_note: 'कोई लाइव अनुरोध एंडपॉइंट तैनाया नहीं है — सभी सेकंड-आँकड़े डेमो-स्केल हैं।',
	corridor_map: 'कॉरिडोर नक्शा',
	corridor_granted_stretch: 'स्वीकृत हिस्सा',
	unit_en_route: 'जन रक्षक यूनिट रास्ते में',
	unit_on_scene: 'जन रक्षक यूनिट मौके पर',
	units_note_fallback: 'लाइव डिस्पैच फ़ीड तैनाया नहीं — यूनिट स्थिति स्क्रिप्टेड डेमो से।',
	control_room: 'कंट्रोल रूम',
	control_room_line: '108 कंट्रोल · चैनल पर बने रहें',
	after_action: (n) => `दर्ज 5–20 मिनट के ठहराव की तुलना में ~${n} मिनट बचे`,
	map_note_fallback: 'घटना डेटा से कॉरिडोर मार्ग — लाइव जंक्शन स्थिति तैनाया नहीं।',

	// ── /comms ──────────────────────────────────────────────────────────
	comms_title: 'सलाह डेस्क',
	comms_sub: 'एक तथ्य, हर श्रोता, हर भाषा — रिकॉर्ड के लिए लिखा जाता है, स्वतः नहीं भेजा जाता।',
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
		'कोई संदेश गेटवे तैनाया नहीं है। सलाह रिकॉर्ड के लिए लिखी जाती है — डिलीवरी भविष्य का एकीकरण है, ईमानदारी से दिखाया गया है।',
	comms_dispatch_threads: 'डिस्पैच थ्रेड',
	comms_dispatch_live: 'लाइव',
	comms_dispatch_demo: 'स्क्रिप्टेड डेमो',
	comms_dispatch_none: 'इस घटना के लिए अभी कोई डिस्पैच दर्ज नहीं।',
	comms_all_clear: 'सभी-साफ़ ड्राफ़्ट करें',
	comms_audit_echo: 'सलाह तैयार होना निर्णय ऑडिट स्ट्रीम में दर्ज होता है।',

	adv_citizen_hold: (kind, minutes, clock, alt) =>
		`${kind}: मार्ग पर ~${minutes} मिनट का ठहराव ${clock}। ${alt} इस्तेमाल करें। सड़कें समय पर खुलेंगी।`,
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
};

// ─── GU ─────────────────────────────────────────────────────────────────────

const gu: Pack = {
	// ── /citizen ────────────────────────────────────────────────────────
	citizen_kicker: 'તમારો રસ્તો, આગામી 2 કલાક',
	citizen_explainer_link: 'રસ્તા કેમ રોકાય છે',
	strip_calm: 'આખા શહેરમાં રસ્તા સામાન્ય ચાલે છે.',
	strip_watch: 'એક ઘટના માર્ગ સક્રિય છે — તેની નજીકે ટૂંકા આયોજિત અવરોધ શક્ય છે.',
	strip_hold: 'નીચેના માર્ગો પર આયોજિત અવરોધ ચાલુ છે. સમયસર ખૂલશે.',
	strip_severe: 'પ્રાથમિકતાવાળો કોરિડો ટ્રાફિક ચાલુ છે. સામાન્ય અવરોધ ટૂંકા રહે છે અને ચોકડી-પ્રતિ-ચોકડી ખૂલે છે.',
	card_hold_label: 'રસ્તો બંધ રહેશે',
	card_window_at: (clock) => `આસપાસ ${clock} વાગ્યે`,
	card_now: 'અત્યારે બંધ',
	card_soon: 'આગામી',
	card_alt_label: 'આ બદલે ચાલો',
	timeline_title: 'આજે તમારા માર્ગ પર',
	timeline_empty: 'આજે બીજી કોઈ કોરિડો ઘટના આયોજિત નથી.',
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

	// ── /driver ─────────────────────────────────────────────────────────
	driver_kicker: 'ગ્રીન કોરિડો',
	golden_hour: 'ગોલ્ડન આવર',
	golden_elapsed: 'વીગ્યો',
	phase_idle: 'નિષ્ક્રિય',
	phase_evaluating: 'મૂલવાઈ રહ્યું',
	phase_proposal: 'પ્રસ્તાવ',
	phase_open: 'વિન્ડો ખુલી',
	phase_deferred: 'ટાળેલ',
	doctrine_note: 'એમ્બ્યુલન્સ ચોકડી પર પહોંચે તેના 60–180 સેકંડ પહેલાં ગ્રીન કોરિડો ખૂલે છે.',
	scripted_badge: 'સ્ક્રિપ્ટેડ ફૉલબેક',
	scripted_note: 'કોઈ લાઇવ વિનંતી એન્ડપોઇન્ટ ડિપ્લોય નથી — બધા સેકંડ-આંકડા ડેમો-સ્કેલ છે.',
	corridor_map: 'કોરિડો નકશો',
	corridor_granted_stretch: 'મંજૂર ટૂકડો',
	unit_en_route: 'જન રક્ષક યુનિટ રસ્તામાં',
	unit_on_scene: 'જન રક્ષક યુનિટ સ્થળ પર',
	units_note_fallback: 'લાઇવ ડિસ્પેચ ફીડ ડિપ્લોય નથી — યુનિટ સ્થિતિ સ્ક્રિપ્ટેડ ડેમોમાંથી.',
	control_room: 'કંટ્રોલ રૂમ',
	control_room_line: '108 કંટ્રોલ · ચેનલ પર રહો',
	after_action: (n) => `નોંધાયેલા 5–20 મિનિટના અવરોધ સામે ~${n} મિનિટ વચ્ચે બચ્યા`,
	map_note_fallback: 'ઘટના ડેટામાંથી કોરિડો માર્ગ — લાઇવ જંક્શન સ્થિતિ ડિપ્લોય નથી.',

	// ── /comms ──────────────────────────────────────────────────────────
	comms_title: 'સલાહ ડેસ્ક',
	comms_sub: 'એક હકીકત, દરેક શ્રોતા, દરેક ભાષા — રેકોર્ડ માટે લખાય, આપોઆપ મોકલાય નહીં.',
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
		'કોઈ મેસેજ ગેટવે ડિપ્લોય નથી. સલાહ રેકોર્ડ માટે લખાય છે — ડિલિવરી ભાવિ ઇન્ટીગ્રેશન છે, પ્રામાણિકતાથી બતાવેલ છે.',
	comms_dispatch_threads: 'ડિસ્પેચ થ્રેડ',
	comms_dispatch_live: 'લાઇવ',
	comms_dispatch_demo: 'સ્ક્રિપ્ટેડ ડેમો',
	comms_dispatch_none: 'આ ઘટના માટે હજુ કોઈ ડિસ્પેચ નોંધાયેલ નથી.',
	comms_all_clear: 'ઑલ-ક્લિયર ડ્રાફ્ટ કરો',
	comms_audit_echo: 'સલાહ તૈયાર થવી એ નિર્ણય ઑડિટ સ્ટ્રીમમાં નોંધાય છે.',

	adv_citizen_hold: (kind, minutes, clock, alt) =>
		`${kind}: માર્ગ પર ~${minutes} મિનિટનો અવરોધ ${clock}. ${alt} વાપરો. રસ્તા સમયસર ખૂલશે.`,
	adv_citizen_clear: (kind) => `${kind} પૂર્ણ. રસ્તા સમયસર ખૂલી ગયા. તમારા સહકાર બદલ આભાર.`,
	adv_zone: (kind, seg, minutes) =>
		`ઝોન અધિકારીઓ: ${kind} — ટૂકડો ${seg} પર ~${minutes} મિનિટ અવરોધ. રોકો, પછી સમયસર છોડો.`,
	adv_108: (kind, seg) =>
		`108 કંટ્રોલ: ${kind} ટૂકડો ${seg} પર સક્રિય. આ ટૂકડાની ગ્રીન-કોરિડો વિનંતિઓના જવાબ અપાઈ રહ્યા છે.`,
	adv_municipal: (kind, seg, minutes) =>
		`મ્યુનિસિપાલિટી: ${kind} ટૂકડો ${seg} પર, ~${minutes} મિનિટના આયોજિત અવરોધ. બેરિકેડ ટીમો ચોકડી-પ્રતિ-ચોકડી છોડે.`,

	// ── /chat ───────────────────────────────────────────────────────────
	chat_title: 'એજન્ટ કન્સોલ',
	chat_sub: 'કોરિડો બ્રેઇન · પસંદ કરેલી ઘટના પર સ્ટ્રીમ્ડ તર્ક',
	chat_context: 'સંદર્ભ',
	chat_context_none: 'કોઈ ઘટના પસંદ નથી — કન્સોલ સંદર્ભ વિના ચાલશે.',
	chat_placeholder: 'કોરિડો વિશે પૂછો…',
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

/** Whether a key is provided by this pack (used by the completeness gate). */
export function isCitizenKey(key: string): boolean {
	return key in en;
}
