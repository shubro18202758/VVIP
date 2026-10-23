/**
 * Agentic God's-eye string pack (en/hi/gu) — the AI-suggests chip and its
 * canvas bus, the agent console's suggestion / citation / directive chips,
 * the command palette (⌘K) and the dossier's "Explain this". Registered
 * additively via registerPack(); the core packs and strings*.ts stay frozen.
 *
 * Import side-effect: every surface that renders these keys imports this
 * module (GodsEyeCanvas, CommandPalette, EntityDossier, ChatConsole).
 *
 * Operational codes stay English inside the strings (URNs, layer ids,
 * sensor modes, plate marks): the brain and the canvas speak them.
 */
import { registerPack } from './strings';

type Pack = Record<string, string | ((...args: unknown[]) => string)>;

const en: Pack = {
	agent_suggests: 'AI suggests',
	agent_apply: 'Apply',
	agent_dismiss: 'Dismiss',
	agent_applied: 'applied',
	agent_dismissed: 'dismissed',
	agent_ack_failed: 'applied here — the audit ack did not reach the brain',
	agent_proposal: 'a proposal — nothing moves until a human applies it',
	agent_from: (tier) => `from the agent (${tier} tier)`,
	agent_more: (n) => `+${n} more`,
	agent_open_map: 'Open on map',
	agent_queue_note: 'AI suggestions wait on the City Command canvas',
	agent_verb_canvas_fly_to: 'fly to',
	agent_verb_canvas_track_entity: 'track',
	agent_verb_canvas_highlight: 'highlight',
	agent_verb_canvas_annotate: 'note',
	agent_verb_canvas_set_sensor: 'sensor',
	agent_verb_canvas_toggle_layer: 'layer',
	agent_note_label: 'AI NOTE',

	chat_suggest_title: 'Try',
	chat_suggest_traffic: 'Live traffic on this corridor',
	chat_suggest_interception: 'Interception plan',
	chat_suggest_closures: 'Closure status',
	chat_suggest_map: 'Show it on the map',
	chat_suggest_stolen: 'Show the stolen vehicle on the map',
	chat_suggest_events: 'Which events are active?',
	chat_suggest_units: 'Where are the units?',
	chat_cited: 'Cited',
	chat_directives: 'Proposed on the map',
	chat_open_city: 'Open in City Command',

	pal_title: 'Command palette',
	pal_placeholder: 'fly to GJ27EM9001 · show police units near Relief Road · layer cameras off · sensor flir-iron · ask …',
	pal_hint: '⌘K / Ctrl+K · ↑↓ choose · Enter runs · Esc closes',
	pal_fly: 'Fly to',
	pal_units: 'Show units',
	pal_layer: 'Layer',
	pal_sensor: 'Sensor',
	pal_ask: 'Ask the agent',
	pal_no_match: 'No vehicle, junction, road or entity matches',
	pal_vehicles_only: 'Vehicles only (ANPR plates) — there is no person search',
	pal_sent: 'Sent to the agent — its suggestions arrive on this canvas',
	pal_units_near: (n, road) => `${n} unit(s) near ${road}`,
	pal_none_near: (road) => `no units near ${road} right now`,
	pal_unknown_road: 'no road by that name on the network',

	dossier_explain: 'Explain this',
	dossier_explaining: 'Explaining…',
	dossier_explain_cancel: 'Cancel',
	dossier_explain_masked: 'plates masked — a named duty officer sees them',
	dossier_explain_failed: 'the agent did not answer',
	dossier_explain_by: 'agent answer',
	dossier_test: 'TEST',
	dossier_session_officer: 'signed-in duty officer',

	canvas_live_paused: 'LIVE paused',
	canvas_live_paused_hint: 'a replay owns the canvas — live layers resume on LIVE',
	canvas_ama_title:
		'Active Movement Adaptation — situation → hysteresis → policy floors → twin shadow-eval (fail-closed) → audited persistence. Age = time since the newest band reading (the event’s latest situation reading, or the city band’s latest engine evaluation) — not how long the band has held. STALE = no new reading for 10 min.',
};

const hi: Pack = {
	agent_suggests: 'AI का सुझाव',
	agent_apply: 'लागू करें',
	agent_dismiss: 'खारिज करें',
	agent_applied: 'लागू किया',
	agent_dismissed: 'खारिज किया',
	agent_ack_failed: 'यहाँ लागू किया — ऑडिट पावती ब्रेन तक नहीं पहुँची',
	agent_proposal: 'केवल प्रस्ताव — मानव के लागू करने तक कुछ नहीं हिलता',
	agent_from: (tier) => `एजेंट से (${tier} स्तर)`,
	agent_more: (n) => `+${n} और`,
	agent_open_map: 'मानचित्र पर खोलें',
	agent_queue_note: 'AI सुझाव सिटी कमांड कैनवास पर प्रतीक्षा करते हैं',
	agent_verb_canvas_fly_to: 'यहाँ जाएँ',
	agent_verb_canvas_track_entity: 'ट्रैक करें',
	agent_verb_canvas_highlight: 'हाइलाइट करें',
	agent_verb_canvas_annotate: 'नोट',
	agent_verb_canvas_set_sensor: 'सेंसर',
	agent_verb_canvas_toggle_layer: 'परत',
	agent_note_label: 'AI नोट',

	chat_suggest_title: 'आज़माएँ',
	chat_suggest_traffic: 'इस कॉरिडोर पर लाइव ट्रैफ़िक',
	chat_suggest_interception: 'इंटरसेप्शन योजना',
	chat_suggest_closures: 'बंद मार्गों की स्थिति',
	chat_suggest_map: 'मानचित्र पर दिखाएँ',
	chat_suggest_stolen: 'चोरी का वाहन मानचित्र पर दिखाएँ',
	chat_suggest_events: 'कौन-सी घटनाएँ सक्रिय हैं?',
	chat_suggest_units: 'यूनिट कहाँ हैं?',
	chat_cited: 'संदर्भ',
	chat_directives: 'मानचित्र पर प्रस्तावित',
	chat_open_city: 'सिटी कमांड में खोलें',

	pal_title: 'कमांड पैलेट',
	pal_placeholder: 'fly to GJ27EM9001 · show police units near Relief Road · layer cameras off · sensor flir-iron · ask …',
	pal_hint: '⌘K / Ctrl+K · ↑↓ चुनें · Enter चलाएँ · Esc बंद करें',
	pal_fly: 'यहाँ जाएँ',
	pal_units: 'यूनिट दिखाएँ',
	pal_layer: 'परत',
	pal_sensor: 'सेंसर',
	pal_ask: 'एजेंट से पूछें',
	pal_no_match: 'कोई वाहन, जंक्शन, सड़क या इकाई मेल नहीं खाती',
	pal_vehicles_only: 'केवल वाहन (ANPR नंबर प्लेट) — किसी व्यक्ति की खोज नहीं होती',
	pal_sent: 'एजेंट को भेजा — उसके सुझाव इसी कैनवास पर आएँगे',
	pal_units_near: (n, road) => `${road} के पास ${n} यूनिट`,
	pal_none_near: (road) => `अभी ${road} के पास कोई यूनिट नहीं`,
	pal_unknown_road: 'नेटवर्क पर इस नाम की कोई सड़क नहीं',

	dossier_explain: 'इसे समझाएँ',
	dossier_explaining: 'समझा रहा है…',
	dossier_explain_cancel: 'रद्द करें',
	dossier_explain_masked: 'प्लेट छिपी हैं — नामित ड्यूटी अधिकारी उन्हें देखते हैं',
	dossier_explain_failed: 'एजेंट ने उत्तर नहीं दिया',
	dossier_explain_by: 'एजेंट का उत्तर',
	dossier_test: 'परीक्षण',
	dossier_session_officer: 'साइन-इन ड्यूटी अधिकारी',

	canvas_live_paused: 'लाइव रुका',
	canvas_live_paused_hint: 'रीप्ले कैनवास पर है — LIVE पर लाइव परतें फिर चलेंगी',
	canvas_ama_title:
		'सक्रिय मूवमेंट अनुकूलन — स्थिति → हिस्टेरिसिस → नीति सीमाएँ → ट्विन शैडो-जाँच (फ़ेल-क्लोज़्ड) → ऑडिटेड भंडारण। आयु = बैंड की सबसे नई रीडिंग के बाद का समय (घटना की ताज़ा स्थिति रीडिंग, या शहर बैंड का ताज़ा इंजन मूल्यांकन) — बैंड कितनी देर से टिका है, वह नहीं। STALE = 10 मिनट से कोई नई रीडिंग नहीं।',
};

const gu: Pack = {
	agent_suggests: 'AI નું સૂચન',
	agent_apply: 'લાગુ કરો',
	agent_dismiss: 'નકારો',
	agent_applied: 'લાગુ કર્યું',
	agent_dismissed: 'નકાર્યું',
	agent_ack_failed: 'અહીં લાગુ કર્યું — ઓડિટ પાવતી બ્રેઇન સુધી ન પહોંચી',
	agent_proposal: 'માત્ર પ્રસ્તાવ — માનવ લાગુ કરે ત્યાં સુધી કંઈ ખસતું નથી',
	agent_from: (tier) => `એજન્ટ તરફથી (${tier} સ્તર)`,
	agent_more: (n) => `+${n} વધુ`,
	agent_open_map: 'નકશા પર ખોલો',
	agent_queue_note: 'AI સૂચનો સિટી કમાન્ડ કેનવાસ પર રાહ જુએ છે',
	agent_verb_canvas_fly_to: 'અહીં જાઓ',
	agent_verb_canvas_track_entity: 'ટ્રેક કરો',
	agent_verb_canvas_highlight: 'હાઇલાઇટ કરો',
	agent_verb_canvas_annotate: 'નોંધ',
	agent_verb_canvas_set_sensor: 'સેન્સર',
	agent_verb_canvas_toggle_layer: 'સ્તર',
	agent_note_label: 'AI નોંધ',

	chat_suggest_title: 'અજમાવો',
	chat_suggest_traffic: 'આ કોરિડોર પર લાઇવ ટ્રાફિક',
	chat_suggest_interception: 'ઇન્ટરસેપ્શન યોજના',
	chat_suggest_closures: 'બંધ માર્ગોની સ્થિતિ',
	chat_suggest_map: 'નકશા પર બતાવો',
	chat_suggest_stolen: 'ચોરાયેલું વાહન નકશા પર બતાવો',
	chat_suggest_events: 'કઈ ઘટનાઓ સક્રિય છે?',
	chat_suggest_units: 'યુનિટ ક્યાં છે?',
	chat_cited: 'સંદર્ભ',
	chat_directives: 'નકશા પર પ્રસ્તાવિત',
	chat_open_city: 'સિટી કમાન્ડમાં ખોલો',

	pal_title: 'કમાન્ડ પેલેટ',
	pal_placeholder: 'fly to GJ27EM9001 · show police units near Relief Road · layer cameras off · sensor flir-iron · ask …',
	pal_hint: '⌘K / Ctrl+K · ↑↓ પસંદ કરો · Enter ચલાવો · Esc બંધ કરો',
	pal_fly: 'અહીં જાઓ',
	pal_units: 'યુનિટ બતાવો',
	pal_layer: 'સ્તર',
	pal_sensor: 'સેન્સર',
	pal_ask: 'એજન્ટને પૂછો',
	pal_no_match: 'કોઈ વાહન, જંકશન, રસ્તો કે એકમ મેળ ખાતું નથી',
	pal_vehicles_only: 'માત્ર વાહનો (ANPR નંબર પ્લેટ) — કોઈ વ્યક્તિની શોધ થતી નથી',
	pal_sent: 'એજન્ટને મોકલ્યું — તેના સૂચનો આ કેનવાસ પર આવશે',
	pal_units_near: (n, road) => `${road} પાસે ${n} યુનિટ`,
	pal_none_near: (road) => `અત્યારે ${road} પાસે કોઈ યુનિટ નથી`,
	pal_unknown_road: 'નેટવર્ક પર આ નામનો કોઈ રસ્તો નથી',

	dossier_explain: 'આ સમજાવો',
	dossier_explaining: 'સમજાવી રહ્યું છે…',
	dossier_explain_cancel: 'રદ કરો',
	dossier_explain_masked: 'પ્લેટ છુપાયેલી છે — નામિત ડ્યુટી અધિકારી તેને જુએ છે',
	dossier_explain_failed: 'એજન્ટે જવાબ ન આપ્યો',
	dossier_explain_by: 'એજન્ટનો જવાબ',
	dossier_test: 'પરીક્ષણ',
	dossier_session_officer: 'સાઇન-ઇન ડ્યુટી અધિકારી',

	canvas_live_paused: 'લાઇવ થોભ્યું',
	canvas_live_paused_hint: 'રીપ્લે કેનવાસ પર છે — LIVE પર લાઇવ સ્તરો ફરી ચાલશે',
	canvas_ama_title:
		'સક્રિય મૂવમેન્ટ અનુકૂલન — સ્થિતિ → હિસ્ટેરેસિસ → નીતિ મર્યાદાઓ → ટ્વિન શેડો-તપાસ (ફેલ-ક્લોઝ્ડ) → ઑડિટેડ સંગ્રહ. ઉંમર = બેન્ડના સૌથી નવા રીડિંગ પછીનો સમય (ઘટનાનું તાજું સ્થિતિ રીડિંગ, અથવા શહેર બેન્ડનું તાજું એન્જિન મૂલ્યાંકન) — બેન્ડ કેટલા સમયથી ટક્યો છે તે નહીં. STALE = 10 મિનિટથી કોઈ નવું રીડિંગ નથી.',
};

registerPack('en', en);
registerPack('hi', hi);
registerPack('gu', gu);

/** Every key this pack defines (tests assert en/hi/gu parity). */
export const AGENT_PACK_KEYS: readonly string[] = Object.keys(en);
export const AGENT_PACKS = { en, hi, gu } as const;
