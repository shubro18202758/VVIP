/**
 * Citizen Advisory Layer — trilingual string packs (EN / HI / GU).
 *
 * Public-facing copy only. Road names stay transliterated (as they appear on
 * city signage) across all languages. Add languages by appending a pack and
 * registering its code in SUPPORTED.
 */

export const SUPPORTED = [
  { code: 'en', label: 'English' },
  { code: 'hi', label: 'हिंदी' },
  { code: 'gu', label: 'ગુજરાતી' },
];

const PACKS = {
  en: {
    brand: 'Corridor Alerts · Ahmedabad',
    tagline: 'Know before the barricade goes up.',
    updated: 'Updated live from the traffic control room',
    statusActive: 'Protected movement in progress',
    statusActiveSub: 'Rolling holds of ~2–3 minutes per stretch — not a full freeze.',
    statusClear: 'No active corridor restrictions',
    statusClearSub: 'Normal traffic across all city corridors.',
    statusDone: 'Movement completed — corridor released',
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
      'Closures are timed to the convoy’s live position (60–180 s ahead) using traffic forecasting — the corridor freezes only while the movement actually passes.',
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
      'Request a green-corridor window through the active movement — the control room answers in seconds.',
    emergencyCta: 'Request green corridor',
    backToOps: 'Operator console',
    footerNote:
      'Illustrative public advisory built on the VVIP Corridor Orchestration prototype. Alternate routes are indicative.',

    // ── Green-corridor driver flow (108) ──────────────────────────
    driverTitle: 'Green Corridor — 108 driver',
    driverSub: 'One tap. The control room answers in seconds.',
    driverBack: 'Citizen alerts',
    noMovement: 'No active corridor',
    noMovementSub: 'No protected movement right now — drive normally. Start the demo movement from the citizen page to try this flow.',
    atStretch: 'You are at',
    urgency: 'Patient urgency',
    urgCardiac: 'Cardiac',
    urgTrauma: 'Trauma',
    urgStroke: 'Stroke',
    urgOther: 'Other emergency',
    requestBtn: 'Request corridor window',
    evaluating: 'Evaluating corridor gap…',
    proposalTitle: 'Window proposed by the platform',
    convoyReachesIn: 'Convoy reaches your stretch in',
    crossBefore: 'Cross before',
    officerLabel: 'Duty officer (demo)',
    approveBtn: 'Approve · open window',
    deferBtn: 'Defer',
    windowOpen: 'WINDOW OPEN — CROSS NOW',
    windowClosesIn: 'Window closes in',
    minutesReturned: 'minutes returned to the golden hour',
    holdPosition: 'Hold position — next window at',
    baselineNote: 'Without a request channel: documented ambulance holds behind convoy corridors run 5–20 minutes.',
    openDriverView: 'Open the 108 driver view',
    demoNote: 'Demo flow driven by the scripted simulation — no real dispatch, no real patient data.',
  },

  hi: {
    brand: 'कॉरिडोर अलर्ट · अहमदाबाद',
    tagline: 'बैरिकेड लगने से पहले ही जानें।',
    updated: 'ट्रैफ़िक कंट्रोल रूम से सीधे अपडेट',
    statusActive: 'सुरक्षित वाहन काफ़िला चल रहा है',
    statusActiveSub: 'हर हिस्से पर ~2–3 मिनट के छोटे रोक — पूरी सड़क बंद नहीं।',
    statusClear: 'इस समय कोई सड़क प्रतिबंध नहीं',
    statusClearSub: 'शहर के सभी कॉरिडोर पर सामान्य यातायात।',
    statusDone: 'काफ़िला पार — कॉरिडोर खुल गया',
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
      'रोक काफ़िले की लाइव स्थिति (60–180 सेकंड पहले) और ट्रैफ़िक पूर्वानुमान से तय होती है — सड़क सिर्फ़ तब बंद रहती है जब काफ़िला सच में पास हो।',
    subscribeTitle: 'अपने फ़ोन पर अलर्ट पाएँ',
    subscribeSub: 'SMS / WhatsApp, निःशुल्क, आपकी भाषा में।',
    phoneLabel: 'मोबाइल नंबर',
    phonePlaceholder: '10 अंकों का नंबर',
    languageLabel: 'अलर्ट की भाषा',
    subscribeBtn: 'सब्सक्राइब करें',
    demoBadge: 'डेमो पूर्वावलोकन — कोई असली SMS/WhatsApp नहीं भेजा जाता',
    previewTitle: 'आपको मिलेगा:',
    subscribed: 'सब्सक्राइब हुआ (डेमो)',
    emergencyTitle: 'एम्बुलेंस या फ़ायर वाहन चला रहे हैं?',
    emergencyBody:
      'चालू काफ़िले से ग्रीन-कॉरिडोर समय माँगें — कंट्रोल रूम सेकंडों में जवाब देता है।',
    emergencyCta: 'ग्रीन कॉरिडोर का अनुरोध',
    backToOps: 'ऑपरेटर कंसोल',
    footerNote:
      'VVIP कॉरिडोर प्रोटोटाइप पर बना नमूना सूचना पटल। वैकल्पिक मार्ग सांकेतिक हैं।',

    // ── Green-corridor driver flow (108) ──────────────────────────
    driverTitle: 'ग्रीन कॉरिडोर — 108 ड्राइवर',
    driverSub: 'एक टैप। कंट्रोल रूम सेकंडों में जवाब देता है।',
    driverBack: 'नागरिक अलर्ट',
    noMovement: 'कोई कॉरिडोर सक्रिय नहीं',
    noMovementSub: 'अभी कोई सुरक्षित वाहन नहीं — सामान्य चलें। यह फ़्लो आज़माने के लिए नागरिक पेज से डेमो काफ़िला चलाएँ।',
    atStretch: 'आप यहाँ हैं',
    urgency: 'मरीज़ की तात्कालिकता',
    urgCardiac: 'हृदय',
    urgTrauma: 'दुर्घटना',
    urgStroke: 'स्ट्रोक',
    urgOther: 'अन्य आपात',
    requestBtn: 'कॉरिडोर समय का अनुरोध',
    evaluating: 'कॉरिडोर गैप आँका जा रहा है…',
    proposalTitle: 'प्लेटफ़ॉर्म का प्रस्तावित समय',
    convoyReachesIn: 'काफ़िला आपके हिस्से तक पहुँचेगा',
    crossBefore: 'इससे पहले पार करें',
    officerLabel: 'ड्यूटी अधिकारी (डेमो)',
    approveBtn: 'मंज़ूर · विंडो खोलें',
    deferBtn: 'टालें',
    windowOpen: 'विंडो खुली — अभी पार करें',
    windowClosesIn: 'विंडो बंद होगी',
    minutesReturned: 'मिनट गोल्डन आवर को लौटाए गए',
    holdPosition: 'रुकें — अगली विंडो',
    baselineNote: 'अनुरोध का कोई रास्ता न हो तो: दर्ज आँकड़ों में एम्बुलेंस 5–20 मिनट रुकती है।',
    openDriverView: '108 ड्राइवर व्यू खोलें',
    demoNote: 'स्क्रिप्टेड सिमुलेशन पर चलने वाला डेमो — कोई असली डिस्पैच नहीं।',
  },

  gu: {
    brand: 'કોરિડોર એલર્ટ · અમદાવાદ',
    tagline: 'અવરોધક ઊભો થાય તે પહેલાં જાણો.',
    updated: 'ટ્રાફિક કંટ્રોલ રૂમમાંથી સીધા અપડેટ',
    statusActive: 'સુરક્ષિત વાહન કાફલો ચાલુ છે',
    statusActiveSub: 'દરેક ટૂકડા પર ~2–3 મિનિટના ટૂંકા અવરોધ — આખો રસ્તો બંધ નહીં.',
    statusClear: 'અત્યારે કોઈ રસ્તા પ્રતિબંધ નથી',
    statusClearSub: 'શહેરના બધા કોરિડોર પર સામાન્ય ટ્રાફિક.',
    statusDone: 'કાફલો પસાર — કોરિડોર ખુલ્લો',
    statusDoneSub: 'ટ્રાફિક સામાન્ય થઈ રહ્યો છે.',
    startDemo: 'ડેમો કાફલો ચલાવો',
    freezeTitle: 'ક્યાં અવરોધ શક્ય છે',
    freezeZone: 'રસ્તાનો ટૂકડો',
    freezeWindow: 'અવરોધનો સમય',
    freezeMinutes: (m) => `~${m} મિનિટ`,
    alternate: 'આ બદલે ચાલો',
    holdNow: 'અત્યારે બંધ',
    passed: 'ખુલ્લો',
    why: 'આટલો ટૂંકો અવરોધ કેમ?',
    whyBody:
      'અવરોધ કાફલાની લાઇવ સ્થિતિ (60–180 સેકંડ પહેલાં) અને ટ્રાફિક આગાહી પરથી નક્કી થાય છે — રસ્તો માત્ર ત્યારે જ બંધ રહે છે જ્યારે કાફલો સાચે જ પાસે હોય.',
    subscribeTitle: 'તમારા ફોન પર એલર્ટ મેળવો',
    subscribeSub: 'SMS / WhatsApp, મફત, તમારી ભાષામાં.',
    phoneLabel: 'મોબાઇલ નંબર',
    phonePlaceholder: '10 આંકડાનો નંબર',
    languageLabel: 'એલર્ટની ભાષા',
    subscribeBtn: 'સબ્સ્ક્રાઇબ કરો',
    demoBadge: 'ડેમો પ્રીવ્યૂ — કોઈ અસલી SMS/WhatsApp મોકલાતો નથી',
    previewTitle: 'તમને આ મળે:',
    subscribed: 'સબ્સ્ક્રાઇબ થયું (ડેમો)',
    emergencyTitle: 'એમ્બ્યુલન્સ કે ફાયર વાહન ચલાવો છો?',
    emergencyBody:
      'ચાલુ કાફલા માટે ગ્રીન-કોરિડોર સમય માંગો — કંટ્રોલ રૂમ સેકંડોમાં જવાબ આપે છે.',
    emergencyCta: 'ગ્રીન કોરિડોર વિનંતી',
    backToOps: 'ઓપરેટર કન્સોલ',
    footerNote:
      'VVIP કોરિડોર પ્રોટોટાઇપ પર બનેલું નમૂનારૂપ માહિતી પેનલ. વૈકલ્પિક માર્ગો સૂચક છે.',

    // ── Green-corridor driver flow (108) ──────────────────────────
    driverTitle: 'ગ્રીન કોરિડોર — 108 ડ્રાઇવર',
    driverSub: 'એક ટેપ. કંટ્રોલ રૂમ સેકંડોમાં જવાબ આપે છે.',
    driverBack: 'નાગરિક એલર્ટ',
    noMovement: 'કોઈ કોરિડોર સક્રિય નથી',
    noMovementSub: 'અત્યારે કોઈ સુરક્ષિત વાહન નથી — સામાન્ય ચલાવો. આ ફ્લો અજમાવવા નાગરિક પેજ પરથી ડેમો કાફલો ચલાવો.',
    atStretch: 'તમે અહીં છો',
    urgency: 'દર્દીની તાત્કાલિકતા',
    urgCardiac: 'હૃદય',
    urgTrauma: 'અકસ્માત',
    urgStroke: 'સ્ટ્રોક',
    urgOther: 'અન્ય કટોકટી',
    requestBtn: 'કોરિડોર સમયની વિનંતી',
    evaluating: 'કોરિડોર ગેપ મૂલવાઈ રહી છે…',
    proposalTitle: 'પ્લેટફોર્મનો સૂચવેલો સમય',
    convoyReachesIn: 'કાફલો તમારા ટૂકડા પર પહોંચશે',
    crossBefore: 'આ પહેલાં પસાર કરો',
    officerLabel: 'ડ્યુટી ઓફિસર (ડેમો)',
    approveBtn: 'મંજૂર · વિન્ડો ખોલો',
    deferBtn: 'ટાળો',
    windowOpen: 'વિન્ડો ખુલ્લી — હમણાં પસાર કરો',
    windowClosesIn: 'વિન્ડો બંધ થશે',
    minutesReturned: 'મિનિટ ગોલ્ડન આવરને પરત',
    holdPosition: 'રોકાઓ — આગામી વિન્ડો',
    baselineNote: 'વિનંતીનો કોઈ રસ્તો ન હોય તો: દર્જ આંકડામાં એમ્બ્યુલન્સ 5–20 મિનિટ રોકાય છે.',
    openDriverView: '108 ડ્રાઇવર વ્યૂ ખોલો',
    demoNote: 'સ્ક્રિપ્ટેડ સિમ્યુલેશન પર ચાલતો ડેમો — કોઈ અસલી ડિસ્પેચ નથી.',
  },
};

export function t(lang, key, ...args) {
  const pack = PACKS[lang] || PACKS.en;
  const val = pack[key];
  if (typeof val === 'function') return val(...args);
  return val != null ? val : key;
}

/**
 * Indic-script digits for the demo alert preview (authentic touch — Indian
 * SMS gateways deliver in the subscriber's script).
 */
const DIGITS = {
  en: '0123456789',
  hi: '०१२३४५६७८९',
  gu: '૦૧૨૩૪૫૬૭૮૯',
};

export function localizeNumber(n, lang) {
  const table = DIGITS[lang] || DIGITS.en;
  return String(n).replace(/[0-9]/g, (d) => table[+d]);
}
