# VVIP Convoy Command — Frontend Documentation

> **Project:** AI-Enabled Intelligent Mobility & Convoy Corridor Optimization System
> **Platform:** Gujarat Police — Integrated Mobility Intelligence Platform
> **Frontend Version:** 2.0 (Light Theme)
> **Stack:** React 19 (JSX), Google Maps JavaScript API, Inter + JetBrains Mono
> **File:** `vvip-dashboard-v2.jsx`
> **Prepared for:** Engineering Team (Frontend + Backend)
> **Date:** March 2026

---

## Table of Contents

1. [Project Concept & Purpose](#1-project-concept--purpose)
2. [Design Language](#2-design-language)
3. [Application Architecture](#3-application-architecture)
4. [Component Breakdown](#4-component-breakdown)
   - 4.1 [useGoogleMaps — Custom Hook](#41-usegooglemaps--custom-hook)
   - 4.2 [ConvoyMap — Google Maps Component](#42-convoymap--google-maps-component)
   - 4.3 [NotificationBell — Clearance Alerts](#43-notificationbell--clearance-alerts)
   - 4.4 [SearchBar — Global Search](#44-searchbar--global-search)
   - 4.5 [SHdr — Section Header](#45-shdr--section-header)
   - 4.6 [VVIPDashboard — Root Component](#46-vvipconvoy--root-component)
5. [Top Bar](#5-top-bar)
6. [Left Panel — Convoy Planner & Profile](#6-left-panel--convoy-planner--profile)
7. [Center — Google Maps Canvas](#7-center--google-maps-canvas)
8. [Right Panel — Operations Monitor](#8-right-panel--operations-monitor)
9. [Panel Toggle System](#9-panel-toggle-system)
10. [React State Reference](#10-react-state-reference)
11. [Data Structures](#11-data-structures)
12. [Backend API Requirements](#12-backend-api-requirements)
13. [WebSocket Event Schema](#13-websocket-event-schema)
14. [What Is Live vs. What Needs Backend](#14-what-is-live-vs-what-needs-backend)
15. [Future Feature Placeholders](#15-future-feature-placeholders)

---

## 1. Project Concept & Purpose

### 1.1 The Problem

VVIP convoys in Gujarat — covering Chief Minister movements, cabinet ministers, dignitaries, and high-security operations — currently suffer from three core gaps:

1. **No unified planning tool:** Route planning, traffic control point deployment, and vehicle assignment happen across disconnected channels (phone calls, radio, manual coordination).
2. **No live situational awareness:** There is no single screen showing where a convoy is, which checkpoints are active, what the traffic looks like, and who is coordinating across agencies.
3. **No clearance workflow:** Emergency vehicles (ambulances, fire trucks) and routine personnel (police escorts, traffic support) have no structured way to request corridor clearance during a VVIP movement — requests arrive informally and can be missed.

### 1.2 The Solution

The **VVIP Convoy Command Dashboard** is a full-screen, real-time command interface that addresses all three gaps on one screen:

- The **Left Panel** handles pre-operation planning: who the VVIP is, where the convoy goes, which vehicles are assigned, and what the AI-predicted route looks like.
- The **Center Map** shows a live Google Maps view of the Ahmedabad–Gandhinagar corridor with the convoy route drawn, all police stations and traffic posts plotted, and TCP checkpoints marked.
- The **Right Panel** handles live operations: tracking all active convoy movements in real time, monitoring TCP checkpoint status, coordinating with five agencies, and processing clearance requests from emergency vehicles.
- The **Notification Bell** in the top bar is a dedicated emergency clearance system — ambulances and fire trucks can request corridor clearance and duty officers can APPROVE or DEFER directly from the notification panel.
- The **Search Bar** gives operators instant access to any convoy by ID or any known location on the corridor without navigating away.

### 1.3 Who Uses It

| Role | Primary Use |
|---|---|
| Duty Officer / DSP | Plans the convoy, assigns vehicles, approves clearance requests |
| Control Room Operator | Monitors active convoy progress, watches alerts and TCP status |
| Agency Liaison | Checks agency coordination status, uses COMM to reach officers |
| Traffic Police | Reads clearance requests from the right panel or notification bell |

---

## 2. Design Language

### 2.1 Theme

The interface uses a **light, professional theme** — white background, clean card surfaces, and a warm orange-green-white color system. The design is deliberately clean to ensure long-shift readability in control room environments.

### 2.2 Color System

| Color | Hex | Usage |
|---|---|---|
| **Primary Orange** | `#ea580c` | Main accent — CTAs, convoy route, active state, VVIP op code, section dividers |
| **Light Orange** | `#f97316` | Gradient partner for buttons and logo |
| **Primary Green** | `#16a34a` | Active/online/clear/approved states, origin marker, personnel markers |
| **Primary Blue** | `#2563eb` | Police station markers, informational badges, on-duty count |
| **Alert Red** | `#dc2626` | Ambulance alerts, critical notifications, destination marker, maintenance status |
| **Background** | `#f1f5f9` | App-level background (slate-100) |
| **Surface White** | `#ffffff` | Panels, cards, top bar |
| **Soft Surface** | `#f8fafc` | Input fields, vehicle cards, stat cells |
| **Border Default** | `#e2e8f0` | All card and panel borders |
| **Text Primary** | `#1e293b` | All heading and primary text |
| **Text Secondary** | `#64748b` | Labels, officer names, metadata |
| **Text Muted** | `#94a3b8` | Timestamps, placeholders, subtitles |

### 2.3 Semantic Color Badges

All status badges across the entire UI follow this system:

| State | Background | Text | Border |
|---|---|---|---|
| Active / Online / Ready / Green | `#f0fdf4` | `#16a34a` | `#86efac` |
| En Route / Standby / Warning | `#fff7ed` | `#ea580c` | `#fed7aa` |
| Critical / Alert / Maintenance | `#fef2f2` | `#dc2626` | `#fca5a5` |
| Pending / Informational | `#eff6ff` | `#2563eb` | `#bfdbfe` |

### 2.4 Typography

| Font | Usage |
|---|---|
| `Inter` | All UI text — labels, names, descriptions, buttons |
| `JetBrains Mono` | All data values — convoy IDs, coordinates, fuel %, ETA, timestamps, op codes |

Weights used: 400 (body), 500 (data values), 600 (card titles, labels), 700 (section headers, CTAs, badge text).

### 2.5 Spacing & Radius

- Panel padding: `14px` horizontal, `10px` vertical for sections
- Card radius: `8px` for vehicles, stat cards, convoy progress; `6px` for compact list items
- Input radius: `6–7px`
- Badge radius: `20px` (pill shape) for status; `4–6px` for category tags
- Section header accent bar: `3px wide`, `14px tall`, `#ea580c`, `border-radius: 2px`

### 2.6 Motion

| Element | Property | Duration | Easing |
|---|---|---|---|
| Left/Right panel slide | `transform: translateX(±N)` | `300ms` | `cubic-bezier(0.4, 0, 0.2, 1)` |
| Toggle arrow flip | `transform: rotate(180deg)` | `250ms` | ease |
| Coordinate bar reposition | `right` value | `300ms` | `cubic-bezier(0.4, 0, 0.2, 1)` |
| Convoy progress bar | `width` | `1000ms` | ease |
| Live dot pulse | `opacity + scale` loop | `2000ms` | ease |
| Hover states | `border-color`, `background` | `150ms` | ease |

---

## 3. Application Architecture

### 3.1 Visual Layout

```
┌────────────────────────────────────────────────────────────────────┐
│                          TOP BAR (56px)                            │
│  [Logo] [Title]   [SearchBar] [API Key] [Connect]   [Stats][Threat]│
│                                         [Bell] [Clock] [Pulse]    │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  [LEFT PANEL]         [GOOGLE MAPS]              [RIGHT PANEL]    │
│   300px, z:20          full bleed, z:1             290px, z:20    │
│   absolute overlay     always visible             absolute overlay │
│   slides over map                                 slides over map  │
│                                                                    │
│  [◀ toggle z:25]   [bottom HUD: layers + coords]  [▶ toggle z:25]│
└────────────────────────────────────────────────────────────────────┘
```

### 3.2 Critical Architecture Decision — Panels Float Over the Map

The left and right panels are `position: absolute` overlays sitting on top of the Google Maps canvas. They are **not** grid columns. The Google Maps `<div>` always occupies `position: absolute; inset: 0` — meaning it fills the entire viewport body at all times.

When panels are open they cover part of the map. When they close, the map is fully visible underneath. This is intentional — the map is always rendering, never resized.

Panels use `backdrop-filter` is not applied in v2 since they are solid white. The `box-shadow` on panels provides a soft depth edge against the map.

### 3.3 Z-Index Stack

| Z-Index | Element |
|---|---|
| 1 | Google Maps div (full bleed) |
| 10 | Map HUD elements — weather bar, layer toggles, coordinate bar |
| 20 | Left panel, Right panel |
| 25 | Left toggle button, Right toggle button |
| 30 | Top bar |
| 100 | Notification bell dropdown, Search results dropdown |

### 3.4 Component Tree

```
<VVIPDashboard>                  ← Root, holds all state
  <style>                        ← Global CSS (fonts, scrollbars, animations)
  <TopBar>                       ← inline (not a separate component)
    <SearchBar />                ← Standalone component
    <NotificationBell />         ← Standalone component
  <Body>                         ← position: relative, flex: 1
    <ConvoyMap />                ← Google Maps component
    <WeatherHUD />               ← inline
    <LayerToggles />             ← inline
    <CoordBar />                 ← inline
    <LeftToggleBtn />            ← inline
    <LeftPanel>                  ← inline, absolute positioned
      <VVIPProfileCard />        ← inline section
      <ConvoyPlannerForm />      ← inline section
      <VehicleList />            ← inline section
    <RightToggleBtn />           ← inline
    <RightPanel>                 ← inline, absolute positioned
      <OperationsSummaryStats /> ← inline section
      <ActiveConvoys />          ← inline section
      <TCPCheckpoints />         ← inline section
      <AgencyCoordination />     ← inline section
      <ClearanceRequests />      ← inline section
```

---

## 4. Component Breakdown

### 4.1 `useGoogleMaps` — Custom Hook

**File location:** Top of `vvip-dashboard-v2.jsx`
**Type:** React custom hook
**Returns:** `{ loaded: boolean, error: string | null }`

**Purpose:** Manages the entire lifecycle of the Google Maps JavaScript API `<script>` tag. Accepts an `apiKey` string and injects the script dynamically when a valid key is provided.

**Logic flow:**

```
apiKey provided?
  └─ No → return { loaded: false, error: null } (do nothing)
  └─ Yes →
       window.google?.maps already exists?
         └─ Yes → setLoaded(true) immediately (avoid double-loading)
         └─ No →
              Remove existing #gmap-script if present (handle key change)
              Create new <script> tag:
                src = https://maps.googleapis.com/maps/api/js?key={apiKey}&libraries=places,geometry
                async = true
              Attach to document.head
              script.onload → setLoaded(true)
              script.onerror → setError("Invalid or restricted API key")
```

**Why this pattern:** The hook allows the API key to be entered at runtime in the top bar without requiring a page reload or environment variable. It safely handles key changes by removing the previous script before injecting a new one. The `window.google?.maps` check prevents double-injection if the hook re-runs.

**Dependencies that trigger re-run:** `[apiKey]` — only re-runs when the key string changes.

---

### 4.2 `ConvoyMap` — Google Maps Component

**Props:**

| Prop | Type | Description |
|---|---|---|
| `apiKey` | `string` | The Google Maps API key. Empty string = show placeholder. |
| `overlays` | `{ police: boolean, personnel: boolean }` | Controls which marker sets are rendered |
| `routeData` | `{ origin: string, destination: string }` | Addresses for the Directions API route |

**Internal refs:**

| Ref | Purpose |
|---|---|
| `mapRef` | DOM node reference — the `<div>` that Google Maps mounts into |
| `mapInstanceRef` | Stores the live `google.maps.Map` instance for external access |
| `markersRef` | Array of all active `google.maps.Marker` objects — used to clear/redraw on overlay toggle |

**Initialization sequence (`initMap`):**

```
1. Guard: exit if mapRef.current or window.google.maps not available
2. Create google.maps.Map on mapRef.current with:
   - center: { lat: 23.11, lng: 72.60 }  (Ahmedabad–Gandhinagar midpoint)
   - zoom: 12
   - disableDefaultUI: true
   - zoomControl: true, position: RIGHT_CENTER
   - 16-rule custom light style array (overrides default Google styling)
3. Create DirectionsService instance
4. Create DirectionsRenderer with:
   - suppressMarkers: true (custom origin/destination markers used instead)
   - orange polyline (#ea580c), weight 4, opacity 0.9
   - animated FORWARD_CLOSED_ARROW icons every 100px at 50% offset
5. Attach renderer to map
6. Fire DirectionsService.route() with origin and destination from props
7. On "OK" status → renderer.setDirections(result)
8. Call addMarkers(map, overlays)
```

**`addMarkers(map, overlays)` logic:**

```
1. Clear all existing markers: markersRef.current.forEach(m => m.setMap(null))
2. Reset markersRef.current = []
3. Create single shared InfoWindow instance (reused for all marker clicks)
4. If overlays.police === true:
     Render 4 police station markers (blue circles, scale 9)
5. If overlays.personnel === true:
     Render 4 traffic post markers (green circles, scale 8)
6. Always render 3 TCP checkpoint markers (orange diamonds, custom SVG path)
7. Always render origin marker (green circle, scale 11) at Ahmedabad coords
8. Always render destination marker (red circle, scale 11) at Gandhinagar coords
9. Each marker has a click listener → sets InfoWindow content → opens on marker
```

**Overlay toggle re-render:** A `useEffect` watches `[overlays.police, overlays.personnel]`. When either changes, `addMarkers()` re-runs on the existing map instance — clearing and redrawing all markers from scratch with the new overlay state.

**Three render states:**

| State | Condition | What renders |
|---|---|---|
| Placeholder | `apiKey === ""` | Light grey grid background, orange map-pin icon, API key instructions |
| Error | `error !== null` | Red error message with troubleshooting hint |
| Live map | `loaded === true` | Full Google Maps with dark-light styling and all markers |

**Map style highlights (16 rules):**

All geometry is near-white (`#f8fafc`). Roads are soft slate (`#e2e8f0` local, `#cbd5e1` highway). Water bodies are light blue (`#bfdbfe`). Parks are light mint green (`#d1fae5`). POI labels are hidden. The result is a clean, muted base that doesn't compete with the orange route line or colored markers.

---

### 4.3 `NotificationBell` — Clearance Alerts

**Props:**

| Prop | Type | Description |
|---|---|---|
| `alerts` | `Alert[]` | Full clearance alert array from parent state |
| `open` | `boolean` | Whether the dropdown panel is visible |
| `onToggle` | `() => void` | Callback to toggle open/closed |

**Purpose:** Handles emergency vehicle and personnel clearance requests. Ambulances, fire trucks, police units, and traffic personnel can request corridor clearance during a VVIP movement. This component provides the notification entry point in the top bar and the action interface to approve or defer each request.

**Bell icon behavior:**

- Renders a bell SVG icon inside a `38×38px` rounded button
- If `open === true`: button background becomes `#fff7ed` (orange tint), icon stroke becomes `#ea580c`
- If any unread alerts exist: a red badge overlaid on the top-right corner of the button shows the unread count
- Badge: `18×18px` red circle, white text, `border: 2px solid #fff` to lift it visually

**Dropdown panel (rendered when `open === true`):**

- Positioned `top: 46px, right: 0` relative to the bell button
- Width: `340px`, white background, `border-radius: 12px`, box-shadow for elevation
- Header row: "Clearance Requests" title + unread count subtitle + red `N NEW` badge
- Scrollable list: `max-height: 360px`
- Footer: "View all alerts →" text link (placeholder for full alert history page)

**Alert item rendering:**

Each alert item renders:
- A type icon in a rounded square with type-specific background
- Alert message text (font-weight 600 if unread, 400 if read)
- Category tag badge + timestamp
- If `!a.read`: orange dot indicator + APPROVE (green) and DEFER (grey) action buttons
- Background: `#fffbf5` (warm tint) if unread, plain white if read
- Border: `#fed7aa` if unread, `#f8fafc` if read

**Type-to-style mapping:**

| Type | Icon | Tag color | Background |
|---|---|---|---|
| `ambulance` | 🚑 | `#dc2626` (red) | `#fef2f2` |
| `firetruck` | 🚒 | `#ea580c` (orange) | `#fff7ed` |
| `police` | 🚔 | `#2563eb` (blue) | `#eff6ff` |
| `traffic` | 🚦 | `#16a34a` (green) | `#f0fdf4` |

**Close behavior:** The parent `VVIPDashboard` attaches an `onClick` to the body div that calls `setNotifOpen(false)` — clicking anywhere outside the bell/dropdown closes it. The bell's `onToggle` calls `setNotifOpen(o => !o)`.

---

### 4.4 `SearchBar` — Global Search

**Props:**

| Prop | Type | Description |
|---|---|---|
| `convoys` | `Convoy[]` | Active convoy list for search indexing |
| `onSelect` | `(item) => void` | Callback when user selects a result |

**Purpose:** Provides instant search across all active convoys (by ID) and all known locations on the Ahmedabad–Gandhinagar corridor.

**Internal state:**

| State | Type | Description |
|---|---|---|
| `query` | `string` | Current text input value |
| `focused` | `boolean` | Whether the input is focused — controls dropdown visibility |

**Search index construction:**

```js
const allItems = [
  ...convoys.map(c => ({ ...c, kind: "convoy" })),
  { id: "L1", label: "Raj Bhavan, Ahmedabad", kind: "location" },
  { id: "L2", label: "Secretariat, Gandhinagar", kind: "location" },
  { id: "L3", label: "Ahmedabad Airport", kind: "location" },
  { id: "L4", label: "Circuit House, Gandhinagar", kind: "location" },
  { id: "L5", label: "Sachivalaya, Gandhinagar", kind: "location" },
  { id: "L6", label: "Gujarat Assembly, Gandhinagar", kind: "location" },
]
```

**Filter logic:**

```js
const results = query.trim().length > 0
  ? allItems.filter(i => i.label.toLowerCase().includes(query.toLowerCase()))
  : []
```

No results shown when query is empty — the dropdown only appears on active typing.

**Dropdown behavior:**

- Opens when `focused === true` AND `results.length > 0`
- Uses `onFocus` → `setFocused(true)` and `onBlur` → `setTimeout(() => setFocused(false), 150)` — the 150ms delay lets `onMouseDown` on a result fire before the blur closes the dropdown
- Each result: type icon square + label + kind sub-label (convoy shows status, location shows "Location")
- Hover: `background: #f8fafc`
- Selection: `onMouseDown` → calls `onSelect(result)`, sets input value to `result.label`

**Clear button:** Appears when `query !== ""` — an × button resets `query` to `""`

**Border highlight:** Input border transitions to `#fb923c` (orange) on focus via inline style swap on `onFocus`/`onBlur`

---

### 4.5 `SHdr` — Section Header

**Props:**

| Prop | Type | Default | Description |
|---|---|---|---|
| `title` | `string` | required | Section name (uppercase, letter-spaced) |
| `badge` | `string` | undefined | Optional right-side status badge text |
| `badgeColor` | `"orange" \| "green" \| "red" \| "blue"` | `"orange"` | Badge color scheme |

**Purpose:** Reusable sticky section header used across both panels. Renders a left orange accent bar, uppercase title, and optional colored pill badge.

**Badge color map:**

| Value | Background | Text | Border |
|---|---|---|---|
| `orange` | `#fff7ed` | `#ea580c` | `#fed7aa` |
| `green` | `#f0fdf4` | `#16a34a` | `#86efac` |
| `red` | `#fef2f2` | `#dc2626` | `#fca5a5` |
| `blue` | `#eff6ff` | `#2563eb` | `#bfdbfe` |

**Sticky behavior:** `position: sticky; top: 0; z-index: 2` — ensures section headers stay pinned while scrolling within the panel. Background is solid white to prevent content from showing through.

---

### 4.6 `VVIPDashboard` — Root Component

The root component owns all application state and renders the full layout. All data, all event handlers, and all child components are managed here. It is the single source of truth for the entire dashboard.

See [Section 10 — React State Reference](#10-react-state-reference) for the full state inventory.

---

## 5. Top Bar

**Height:** 56px | **Background:** `#fff` | **Border-bottom:** `1px solid #e2e8f0` | **Z-index:** 30

The top bar is divided into three horizontal zones: branding (left), search + API key (center), and status strip (right).

### 5.1 Branding Zone

| Element | Detail |
|---|---|
| Logo | 34×34px orange gradient square (`#ea580c → #f97316`), `border-radius: 8px`, SVG layers icon in white |
| Title | "VVIP Convoy Command" — Inter 700, 14px, `#1e293b` |
| Subtitle | "Gujarat Police · Mobility Intelligence Platform" — Inter 400, 10px, `#94a3b8` |

### 5.2 Center Zone — Search + API Key

Contains `<SearchBar>` (280px wide) + vertical divider + API key input (220px) + Connect button.

**API Key connect logic:**

When the user clicks "Connect Map":
1. Trims whitespace from `apiInput`
2. Calls `setApiKey(apiInput.trim())`
3. `apiKey` state change triggers `useGoogleMaps` hook re-run
4. Hook removes old `#gmap-script`, injects new script tag with the new key
5. On load success → `ConvoyMap.initMap()` fires → live map renders

### 5.3 Right Zone — Status Strip

**Live Stat Counters:**

| Counter | Label | Color | Source |
|---|---|---|---|
| `3` | Convoys | `#ea580c` | Hardcoded — replace with `GET /api/convoys/active/count` |
| `18` | On Duty | `#16a34a` | Hardcoded — replace with `GET /api/personnel/on-duty/count` |

**Threat Level Selector:**

Three pill buttons — ALPHA / BRAVO / CHARLIE. Clicking any pill sets `threatLevel` state. The active pill receives its color scheme; inactive pills are grey. This is purely local state — needs `PATCH /api/operations/{id}/threat-level` for persistence and broadcast.

**Notification Bell:** `<NotificationBell>` component — see Section 4.3.

**Live Clock:** `clock` state updated every 1000ms via `setInterval`. Displays 24h time (`toLocaleTimeString('en-IN', { hour12: false })`) and date (`weekday short + day + month short`).

**Live Pulse Dot:** 7×7px green circle with `box-shadow: 0 0 0 2px #bbf7d0` and `pulse` animation — indicates the system is live.

---

## 6. Left Panel — Convoy Planner & Profile

**Width:** 300px | **Background:** `#fff` | **Border-right:** `1px solid #e2e8f0`
**Box-shadow:** `2px 0 12px rgba(0,0,0,0.04)`
**Scrollable** via `.sp` class (custom 4px scrollbar in `#e2e8f0`)

The left panel is the **pre-operation planning workspace**. It stacks four sections from top to bottom.

### 6.1 VVIP Profile Section

**Purpose:** Shows the identity, security classification, and key operation parameters of the VIP being escorted.

**Layout:**

```
┌──────────────────────────────────────────┐
│  [👤 Avatar]   Hon. CM of Gujarat        │
│                Chief Minister's Office   │
│                [Z+ SECURITY] [ACTIVE OP] │
├──────────────────────────────────────────┤
│  Op Code: ZL-2024   Category: Z+ VIP    │
│  Dep. Time: 14:30   ETA: 14:58 IST      │
└──────────────────────────────────────────┘
```

**Avatar:** 48×48px rounded square, orange gradient background (`#fff7ed → #fed7aa`), orange border. Placeholder emoji — replace with `<img>` from API `photoUrl`.

**Badges:**
- "Z+ SECURITY" — orange pill
- "ACTIVE OP" — green pill

**Op Details Grid:** 2×2 grid of `#f8fafc` mini-cards showing: Op Code, Category, Departure Time, ETA. Values in `JetBrains Mono` 12px.

**Backend data needed:**
```
GET /api/vvip/{operationCode}
→ { name, designation, department, clearanceLevel, operationCode, photoUrl, departureTime, etaTime }
```

### 6.2 Convoy Planner Form

**Purpose:** Allows the duty officer to define the convoy route — origin, waypoints, destination, departure time, and priority level — and submit it to the AI route optimization engine.

**Fields in order:**

**Origin input:**
- Label: "🟢 Origin"
- Bound to `origin` state
- Passed as `routeData.origin` prop to `<ConvoyMap>` — used directly in Google Directions API call
- On blur: border reverts to `#e2e8f0`; on focus: border becomes `#fb923c`

**Waypoints block:**
- Two pre-filled read-only rows: "TCP-1 — Vastrapur Junc." and "TCP-2 — SG Highway Entry"
- Each has a `MANDATORY` orange badge — these are required TCP stops
- "＋ Add Waypoint" dashed button — **placeholder**, no handler yet; needs dynamic `waypoints[]` state

**Destination input:**
- Label: "🔴 Destination"
- Bound to `destination` state
- Passed as `routeData.destination` to `<ConvoyMap>`

**Departure Time:** `<input type="time">` default `14:30` — not yet in state, needs wiring.

**Priority select:** Options: Z+ VVIP, Z Security, Y+ Security — not yet in state.

**AI Route Prediction Box:**

Warm orange-tinted card (`linear-gradient(135deg, #fff7ed, #fff)` background, `#fed7aa` border) showing:

| Field | Value | Color |
|---|---|---|
| ETA | 28 min | `#ea580c` |
| Distance | 34 km | `#ea580c` |
| Optimal Route | NH-48 | `#16a34a` |
| Alt. Delta | +12 min | `#94a3b8` |

Two progress bars below:
- "Corridor Congestion" — 62% fill, orange, label "MODERATE"
- "Signal Clearance" — 100% fill, green, label "READY"

All values are **hardcoded placeholders**. When the backend route optimizer is wired, these must populate from the `POST /api/convoy/plan` response.

**"Generate Optimized Route →" Button:**

Orange gradient button, full width. Currently has no `onClick` handler — needs wiring to `POST /api/convoy/plan`. On success: update AI prediction box values + update map route polyline.

### 6.3 Available Vehicles Section

**Purpose:** Displays all vehicles in the assigned fleet for this operation. Operators click to select/deselect vehicles for convoy assignment.

**Selection logic:**
```js
onClick={() => setSelectedVehicles(s =>
  s.includes(v.id) ? s.filter(x => x !== v.id) : [...s, v.id]
)}
```

**Selected state visual:** Orange border (`#fb923c`), warm orange background (`#fff7ed`), box-shadow `0 2px 8px rgba(234,88,12,0.1)`.

**Vehicle card layout:**
```
[emoji icon]  Vehicle Name          [STATUS badge]
              GJ-01-VV-0001
              Fuel: 94%  Range: 580 km
```

**Fuel color logic:** Fuel % text is `#16a34a` if `> 50`, `#ea580c` if `≤ 50`.

**Vehicle data shape:**
```js
{
  id: number,
  icon: string,          // emoji
  name: string,
  regId: string,         // e.g. "GJ-01-VV-0001"
  status: "ready" | "standby" | "maintenance",
  fuel: number,          // percentage integer
  range: string          // e.g. "580 km"
}
```

**Backend endpoint needed:**
```
GET /api/vehicles/available?operationCode=ZL-2024
→ { vehicles: [...], totalAvailable: 5, totalFleet: 6 }
```

---

## 7. Center — Google Maps Canvas

**Position:** `absolute; inset: 0; z-index: 1` — always fills the full viewport body.

### 7.1 Map Style

Light tactical theme applied via Google Maps `styles[]` array. Key visual decisions:

- All base geometry: `#f8fafc` (near-white) — clean, uncluttered
- Roads: slate tones (`#e2e8f0` to `#cbd5e1`) — visible but subdued
- Water: `#bfdbfe` (light blue) — recognizable without being dominant
- Parks: `#d1fae5` (light mint) — adds context
- POI labels hidden entirely — reduces visual noise during operations
- Text labels: `#475569` (slate-600) — readable without competing with markers

### 7.2 Convoy Route

Drawn by Google Directions API via `DirectionsService.route()`:

| Property | Value |
|---|---|
| Origin | `origin` state (address string → geocoded by Google) |
| Destination | `destination` state |
| Travel mode | `DRIVING` |
| Stroke color | `#ea580c` (orange) |
| Stroke weight | 4px |
| Stroke opacity | 0.9 |
| Animated arrows | `FORWARD_CLOSED_ARROW` every 100px, offset 50% |
| Custom markers | Origin and destination suppressed (`suppressMarkers: true`) — custom markers used |

### 7.3 Map Markers

**Police Stations (4 markers)**

Shape: `google.maps.SymbolPath.CIRCLE`, scale 9, fill `#2563eb`, white stroke 1.5px.
Visible only when `overlays.police === true`.
Click → InfoWindow: "POLICE STATION" label (blue) + station name.

Locations:
| Station | Lat | Lng |
|---|---|---|
| Vastrapur Police Station | 23.0317 | 72.5851 |
| Chandlodia Police Station | 23.0567 | 72.5856 |
| Gandhinagar Sector 21 PS | 23.2156 | 72.6369 |
| Sarkhej Police Station | 23.0732 | 72.5098 |

**Traffic Personnel Posts (4 markers)**

Shape: `CIRCLE`, scale 8, fill `#16a34a`, white stroke 1.5px.
Visible only when `overlays.personnel === true`.
Click → InfoWindow: "TRAFFIC POST" label (green) + post name + officer name.

Posts:
| Post | Officer | Lat | Lng |
|---|---|---|---|
| SG Highway Traffic Post | SI R. Patel | 23.0419 | 72.5311 |
| Sola Cross Road Post | SI K. Sharma | 23.0673 | 72.5562 |
| Koba Circle Post | HC D. Mehta | 23.1091 | 72.5874 |
| Infocity Post | SI A. Desai | 23.1843 | 72.6312 |

**TCP Checkpoints (3 markers, always visible)**

Shape: Custom diamond SVG path `M 0,-11 L 8,0 L 0,11 L -8,0 Z`, fill `#ea580c`, stroke `#fed7aa`.
Always rendered regardless of overlay toggles — TCP positions are mandatory routing information.
Click → InfoWindow: "TCP CHECKPOINT" label (orange) + name + "ACTIVE" in green.

Points:
| Checkpoint | Lat | Lng |
|---|---|---|
| TCP-1 \| Vastrapur Junction | 23.0501 | 72.5398 |
| TCP-2 \| SG Highway Entry | 23.0921 | 72.5712 |
| TCP-3 \| Gandhinagar Gate | 23.1654 | 72.6201 |

**Origin Marker:** Green circle (scale 11) at `23.0225, 72.5714` (Ahmedabad). Always visible.
**Destination Marker:** Red circle (scale 11) at `23.2156, 72.6369` (Gandhinagar). Always visible.

**InfoWindow:** One shared instance reused for all markers. Each marker's `click` listener calls `iw.setContent(html)` then `iw.open(map, marker)`.

### 7.4 Map HUD Elements

**Weather Bar (top center):**

```
☀️  34°C  ·  Wind 14 km/h NE  ·  Vis 12 km  ·  CLEAR CONDITIONS
```
`position: absolute; top: 12; left: 50%; transform: translateX(-50%)`. Semi-transparent white background with blur, soft shadow. All values hardcoded — needs `GET /api/weather/corridor` integration.

**Layer Toggle Buttons (bottom center):**

Two buttons — "Police Stn." and "Personnel" — control `overlays.police` and `overlays.personnel` respectively. Active state: type-specific colored bg/border (blue for police, green for personnel). Inactive: white with grey border. Each click calls `setOverlays(o => ({ ...o, [k]: !o[k] }))`.

**Coordinate Bar (bottom right):**

```
23.0225°N · 72.5714°E · AHM–GNR
```
`position: absolute; bottom: 20`. The `right` CSS property transitions smoothly when the right panel opens/closes: `right: rightOpen ? RWIDTH + 12 : 12` with `transition: right 0.3s cubic-bezier(0.4,0,0.2,1)` — ensuring it never gets hidden behind the panel edge.

---

## 8. Right Panel — Operations Monitor

**Width:** 290px | **Background:** `#fff` | **Border-left:** `1px solid #e2e8f0`
**Box-shadow:** `-2px 0 12px rgba(0,0,0,0.04)`

The right panel is the **live operations feed**. It is read-heavy — operators watch this to understand what is happening across all active movements and respond to incoming requests.

### 8.1 Operations Summary Stats

2×2 grid of colored stat cards. Each card: colored background, `JetBrains Mono` 22px number, 10px grey label.

| Stat | Color | Background | Source |
|---|---|---|---|
| Active Convoys | `#ea580c` | `#fff7ed` | Hardcoded `3` |
| TCP Deployed | `#16a34a` | `#f0fdf4` | Hardcoded `12` |
| Alerts | `#dc2626` | `#fef2f2` | Hardcoded `2` |
| On Duty | `#2563eb` | `#eff6ff` | Hardcoded `18` |

All need live API or WebSocket sourcing.

### 8.2 Active Convoys Progress Cards

**Purpose:** Shows a live progress bar for each active convoy with its route, current completion percentage, and ETA.

**Layout per card:**

```
[left colored border]
ZL-VVIP-07                         [EN ROUTE]
↗ Raj Bhavan → Secretariat
[████████░░░░░░░░░░░░░░░░] gradient progress bar
42% Complete                       ETA: 16 min
```

**Color coding:**
- EN ROUTE → orange left border + orange progress bar
- ARRIVED → green left border + green progress bar

**ETA formula (current placeholder):** `Math.round((100 - progress) * 0.28)` minutes. This approximates a 28-minute full journey. Replace with real ETA from WebSocket telemetry.

**Progress animation:** ZL-VVIP-07's progress is driven by `convoyProgress` state which increments `+0.4` every `600ms` via `setInterval`. The other two are static. All three must be driven by `convoy.position_update` WebSocket events in production.

**Three current convoys:**

| ID | Route | Progress | Status |
|---|---|---|---|
| ZL-VVIP-07 | Raj Bhavan → Secretariat | Live (animated) | EN ROUTE |
| ZL-VVIP-04 | Airport → Circuit House | 78% static | EN ROUTE |
| ZL-SEC-11 | Sachivalaya → Assembly | 100% | ARRIVED |

### 8.3 TCP Checkpoints

**Purpose:** Monitors which Traffic Control Points have confirmed personnel on station versus which are still pending deployment.

Four checkpoints listed. Status badge: green pill "ACTIVE" or blue pill "PENDING".

| TCP | Location | Status |
|---|---|---|
| TCP-1 | Vastrapur Junc. | ACTIVE |
| TCP-2 | SG Highway Entry | ACTIVE |
| TCP-3 | Gandhinagar Gate | ACTIVE |
| TCP-4 | Infocity Circle | PENDING |

Needs `GET /api/tcp/status?operationCode=ZL-2024` and `tcp.status_change` WebSocket events.

### 8.4 Agency Coordination

**Purpose:** Shows which partner agencies are currently connected and provides a communication trigger per agency.

**Per-agency row:**
- Agency name (Inter 600, 12px) + Officer name (10px, grey)
- Green/grey dot + "ONLINE"/"OFFLINE" label
- "COMM" button — currently renders but has no handler

**COMM button behavior needed:** Clicking should open a communication channel to that agency. Implementation options: push notification to agency terminal, initiate VoIP call, or open an internal messaging thread. Backend: `POST /api/comms/channel/{agencyId}/open`.

**Agencies:**

| Agency | Officer | Status |
|---|---|---|
| Ahmedabad Traffic CP | DCP Raghuveer Singh | ONLINE |
| Gujarat Police HQ | SP Vivek Mishra | ONLINE |
| Security Escort Unit | Comdt. R. Joshi | ONLINE |
| Gandhinagar Traffic | PI Haresh Patel | OFFLINE |

Needs `GET /api/agencies/status` and `agency.online_change` WebSocket events.

### 8.5 Clearance Requests (Right Panel Copy)

**Purpose:** Mirrors the top notification bell — shows the first 3 clearance requests directly in the right panel so dispatchers can act without opening the bell dropdown.

Renders the top 3 items from `clearanceAlerts` state using `clearanceAlerts.slice(0, 3)`. Each card has APPROVE (green) and DEFER (grey) action buttons for unread items. Same visual logic as the bell dropdown (see Section 4.3).

This duplication is intentional — the bell is for any operator on the screen, the right panel copy is specifically for the dispatcher whose primary workspace is the right panel.

---

## 9. Panel Toggle System

### 9.1 Left Panel

**Toggle button placement:** `position: absolute`, vertically centered (`top: 50%; transform: translateY(-50%)`).

`left` value transitions with the panel:
```js
left: leftOpen ? LWIDTH : 0   // 300px or 0
transition: left 0.3s cubic-bezier(0.4, 0, 0.2, 1)
```

**Panel slide:**
```css
transform: translateX(0)         /* open */
transform: translateX(-300px)    /* closed */
transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1)
```

**Arrow:** `◀` character in orange. When panel is closed, `transform: rotate(180deg)` makes it point right (→), indicating "open". When open, it points left (←), indicating "collapse".

**Button style:** White background, `border-left: none`, right-side rounded (`border-radius: 0 8px 8px 0`). Hover: `#fff7ed` background + `#fed7aa` border.

### 9.2 Right Panel

Mirrored logic:

`right` value transitions:
```js
right: rightOpen ? RWIDTH : 0   // 290px or 0
```

Panel slide:
```css
transform: translateX(0)        /* open */
transform: translateX(290px)    /* closed */
```

**Button style:** `border-right: none`, left-side rounded (`border-radius: 8px 0 0 8px`).

**Arrow:** `▶` rotates to `◀` when closed.

### 9.3 Coordinate Bar Compensation

The coordinate bar in the map HUD adjusts its `right` position to avoid being hidden behind the right panel:
```js
right: rightOpen ? RWIDTH + 12 : 12   // 302px or 12px
transition: right 0.3s cubic-bezier(0.4, 0, 0.2, 1)
```
Both the panel and coordinate bar use identical easing — they move together.

---

## 10. React State Reference

All state lives in `VVIPDashboard`. No external state management (no Redux, no Zustand). All child components receive data via props.

| Variable | Type | Initial Value | Description |
|---|---|---|---|
| `apiKey` | `string` | `""` | Confirmed active Google Maps key (triggers map load) |
| `apiInput` | `string` | `""` | Unconfirmed text field value in top bar |
| `leftOpen` | `boolean` | `true` | Left panel visible state |
| `rightOpen` | `boolean` | `true` | Right panel visible state |
| `clock` | `Date` | `new Date()` | Live clock, updated every 1000ms |
| `threatLevel` | `"ALPHA"\|"BRAVO"\|"CHARLIE"` | `"BRAVO"` | Current threat classification |
| `overlays` | `{ police: bool, personnel: bool }` | both `true` | Map marker layer visibility |
| `selectedVehicles` | `number[]` | `[1, 2, 3]` | IDs of vehicles selected for the convoy |
| `convoyProgress` | `number` | `42` | Progress % for ZL-VVIP-07 (animated) |
| `origin` | `string` | `"Raj Bhavan, Ahmedabad"` | Route origin — drives Directions API |
| `destination` | `string` | `"Secretariat, Gandhinagar"` | Route destination — drives Directions API |
| `notifOpen` | `boolean` | `false` | Notification bell dropdown visibility |
| `clearanceAlerts` | `Alert[]` | 5 hardcoded items | Emergency clearance request feed |

**Live intervals:**

| Interval | Frequency | Effect |
|---|---|---|
| Clock | 1000ms | `setClock(new Date())` |
| Convoy progress | 600ms | `setConvoyProgress(p => Math.min(100, p + 0.4))` |

Both intervals are created in `useEffect(() => {...}, [])` and return cleanup `clearInterval` functions.

---

## 11. Data Structures

### 11.1 Alert Object

```typescript
interface Alert {
  id: number;
  type: "ambulance" | "firetruck" | "police" | "traffic";
  tag: string;           // Display label e.g. "AMBULANCE", "FIRE DEPT"
  message: string;       // Full clearance request description
  time: string;          // Display time string e.g. "14:24:03"
  read: boolean;         // false = unread, shows action buttons
}
```

### 11.2 Vehicle Object

```typescript
interface Vehicle {
  id: number;
  icon: string;          // Emoji
  name: string;
  regId: string;         // e.g. "GJ-01-VV-0001"
  status: "ready" | "standby" | "maintenance";
  fuel: number;          // 0–100 integer
  range: string;         // e.g. "580 km" or "—"
}
```

### 11.3 Convoy Object (for SearchBar)

```typescript
interface Convoy {
  id: string;            // e.g. "C1"
  label: string;         // e.g. "ZL-VVIP-07"
  status: string;        // e.g. "EN ROUTE"
  prog: number;          // 0–100
}
```

### 11.4 Agency Object

```typescript
interface Agency {
  name: string;
  officer: string;
  online: boolean;
}
```

---

## 12. Backend API Requirements

### 12.1 Full Endpoint List

| Priority | Method | Endpoint | Consuming Component |
|---|---|---|---|
| HIGH | `GET` | `/api/config/maps-key` | Top bar — maps key (avoid exposing in client) |
| HIGH | `GET` | `/api/vvip/{operationCode}` | Left panel — VVIP profile card |
| HIGH | `GET` | `/api/vehicles/available?operationCode=` | Left panel — vehicle list |
| HIGH | `POST` | `/api/convoy/plan` | Left panel — Generate Route button |
| HIGH | `GET` | `/api/convoys/active?operationCode=` | Right panel — convoy progress cards |
| HIGH | `GET` | `/api/alerts?operationCode=&limit=20` | Bell dropdown + right panel clearance section |
| HIGH | `GET` | `/api/tcp/status?operationCode=` | Right panel — TCP list |
| HIGH | `GET` | `/api/agencies/status?operationCode=` | Right panel — agency coordination |
| MEDIUM | `PATCH` | `/api/operations/{id}/threat-level` | Top bar — threat selector |
| MEDIUM | `GET` | `/api/weather/corridor?lat=&lng=` | Map HUD — weather bar |
| MEDIUM | `GET` | `/api/police-stations?corridor=` | Map — police markers |
| MEDIUM | `GET` | `/api/traffic-posts/on-duty?operationCode=` | Map — personnel markers |
| MEDIUM | `PATCH` | `/api/alerts/{alertId}/read` | Bell + right panel — mark alert read |
| LOW | `POST` | `/api/comms/channel/{agencyId}/open` | Right panel — COMM button |
| LOW | `GET` | `/api/convoys/active/count` | Top bar stat counter |
| LOW | `GET` | `/api/personnel/on-duty/count` | Top bar stat counter |

### 12.2 `POST /api/convoy/plan` — Request & Response

```json
// Request body
{
  "operationCode": "ZL-2024",
  "origin": "Raj Bhavan, Ahmedabad",
  "destination": "Secretariat, Gandhinagar",
  "waypoints": [
    { "label": "TCP-1", "location": "Vastrapur Junction, Ahmedabad" },
    { "label": "TCP-2", "location": "SG Highway Entry, Ahmedabad" }
  ],
  "departureTime": "2026-03-12T14:30:00+05:30",
  "priority": "Z+",
  "vehicleIds": [1, 2, 3]
}

// Response
{
  "routeId": "route_abc123",
  "eta": { "minutes": 28, "arrivalTime": "2026-03-12T14:58:00+05:30" },
  "distanceKm": 34.2,
  "optimalHighway": "NH-48",
  "alternativeEtaDeltaMinutes": 12,
  "corridorCongestion": { "level": "MODERATE", "percentage": 62 },
  "signalClearanceStatus": "READY",
  "googleMapsPolyline": "encoded_polyline_string"
}
```

The `googleMapsPolyline` field should be decoded and rendered as a custom `google.maps.Polyline` to replace the default Directions API route — ensuring the AI-optimized path is shown, not Google's default.

### 12.3 Standard Error Response

```json
{
  "error": {
    "code": "OPERATION_NOT_FOUND",
    "message": "Operation ZL-2024 does not exist or is not active",
    "timestamp": "2026-03-12T14:23:00Z"
  }
}
```

### 12.4 Auth Header

All endpoints require `Authorization: Bearer {jwt_token}`. JWT payload: `userId`, `role`, `operationCode`, `clearanceLevel`.

---

## 13. WebSocket Event Schema

### 13.1 Connection URL

```
wss://api.example.com/ws?operationCode=ZL-2024&token={jwt}
```

### 13.2 All Event Types

| Event | Trigger | Frontend Action |
|---|---|---|
| `convoy.position_update` | GPS tick (every 5s) | Update progress bar + ETA for matching `convoyId`; update map marker position |
| `convoy.status_change` | Convoy reaches waypoint / arrives / halts | Update status badge text and color |
| `tcp.status_change` | Officer checks in at TCP | Update TCP row status badge |
| `alert.new` | New clearance request received | Prepend to `clearanceAlerts` state; increment bell unread count |
| `alert.approved` | Alert approved by another operator | Mark alert as read in state |
| `agency.online_change` | Agency connects/disconnects | Update agency row dot + label |
| `threat.level_change` | Any operator changes threat level | Sync `threatLevel` state across all connected clients |
| `weather.update` | Periodic refresh | Update weather bar values |

### 13.3 Key Payloads

```json
// convoy.position_update
{
  "convoyId": "ZL-VVIP-07",
  "progressPercent": 43.2,
  "etaMinutes": 15,
  "currentLat": 23.0845,
  "currentLng": 72.5851,
  "timestamp": "2026-03-12T14:23:40Z"
}

// alert.new
{
  "alertId": "alert_006",
  "type": "ambulance",
  "tag": "AMBULANCE",
  "message": "Ambulance GJ-05-9821 requesting corridor clearance — NH-48 towards VS Hospital.",
  "severity": "CRITICAL",
  "timestamp": "2026-03-12T14:25:01Z"
}

// tcp.status_change
{
  "tcpId": "TCP-4",
  "status": "ACTIVE",
  "officerName": "SI D. Mehta",
  "timestamp": "2026-03-12T14:27:00Z"
}
```

---

## 14. What Is Live vs. What Needs Backend

| Feature | Status | Notes |
|---|---|---|
| Light theme, layout, panel toggles | ✅ Complete | Fully implemented |
| Google Maps loading via runtime API key | ✅ Complete | Works with any valid key |
| Custom dark-light map styling | ✅ Complete | 16-rule style array |
| Convoy route via Directions API | ✅ Complete | Re-draws when origin/destination change |
| Police station markers | ✅ Hardcoded | Needs `GET /api/police-stations` |
| Traffic personnel markers | ✅ Hardcoded | Needs `GET /api/traffic-posts/on-duty` |
| TCP checkpoint markers | ✅ Hardcoded | Needs `GET /api/tcp/status` |
| Marker info popups | ✅ Complete | Functional click → InfoWindow |
| Layer toggle (police/personnel) | ✅ Complete | State-driven re-render |
| VVIP profile card | ✅ UI, hardcoded | Needs `GET /api/vvip/{opCode}` |
| Convoy planner form | ✅ UI, no submit | Needs `POST /api/convoy/plan` wire-up |
| AI prediction box | ✅ UI, hardcoded | Needs plan response data |
| Waypoint "Add" button | ✅ UI only | Needs dynamic `waypoints[]` state + form |
| Vehicle list | ✅ Hardcoded | Needs `GET /api/vehicles/available` |
| Vehicle selection | ✅ Complete | Sends IDs with convoy plan |
| Search bar (convoys + locations) | ✅ Complete | Needs real convoy list from API |
| Notification bell | ✅ Complete | Needs `alert.new` WebSocket |
| Bell APPROVE/DEFER buttons | ✅ UI only | Needs `PATCH /api/alerts/{id}/approve` |
| Convoy progress bars | ✅ Simulated | Needs `convoy.position_update` WebSocket |
| TCP list | ✅ Hardcoded | Needs `GET /api/tcp/status` + WebSocket |
| Agency coordination | ✅ Hardcoded | Needs `GET /api/agencies/status` + WebSocket |
| COMM button | ✅ UI only | Needs `POST /api/comms/channel/{id}/open` |
| Clearance requests (right panel) | ✅ Hardcoded | Needs alert API + WebSocket |
| Threat level selector | ✅ Local state | Needs `PATCH /api/operations/{id}/threat-level` |
| Weather bar | ✅ Hardcoded | Needs `GET /api/weather/corridor` |
| Stat counters (top bar) | ✅ Hardcoded | Needs count endpoints |
| Live clock | ✅ Complete | Browser time, no backend needed |

---

## 15. Future Feature Placeholders

The following features are architecturally anticipated — placeholders exist in the UI — but are not yet implemented:

| # | Feature | Where | What's needed |
|---|---|---|---|
| 1 | VVIP photo | Left panel profile avatar | API photoUrl → `<img>` element |
| 2 | Dynamic waypoints | Left panel planner | `waypoints[]` state array; add/remove UI; pass to plan API |
| 3 | Departure time in state | Left panel planner | Wire `<input type="time">` to `departureTime` state |
| 4 | Priority in state | Left panel planner | Wire `<select>` to `priority` state |
| 5 | Generate route wired | Left panel CTA | `onClick → POST /api/convoy/plan → update map + prediction box` |
| 6 | Live GPS convoy marker | Map center | WebSocket `convoy.position_update → google.maps.Marker.setPosition()` |
| 7 | Real-time traffic layer | Map | `google.maps.TrafficLayer` or ITMS feed overlay |
| 8 | COMM modal | Right panel | Agency communication window (voice/text/radio) |
| 9 | Alert dismiss | Bell + right panel | `PATCH /api/alerts/{id}/read` + update `clearanceAlerts` state |
| 10 | Full alert history | Bell footer link | Dedicated alert log page or drawer |
| 11 | New convoy creation | Not built | Full form flow + `POST /api/convoys/create` |
| 12 | Multi-convoy map | Map | Render multiple simultaneous convoy tracks with different colors |
| 13 | Route replay | Map | Historical convoy track playback |
| 14 | Mission brief PDF export | Top bar | `GET /api/convoy/{id}/brief.pdf` |
| 15 | Auth / Login screen | App level | JWT login, role-based view gating |
| 16 | Role-based rendering | All panels | Hide/disable fields per user role (DUTY_OFFICER vs OPERATOR vs LIAISON) |
| 17 | Mobile/responsive layout | All | Fixed desktop layout — no mobile breakpoints yet |

---

*This document represents the complete frontend specification for VVIP Convoy Command Dashboard v2.0.*
*For questions on the frontend implementation, refer to `vvip-dashboard-v2.jsx`.*
*For backend integration queries, refer to Section 12 (API Contracts) and Section 13 (WebSocket Schema).*
