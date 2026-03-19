<script lang="ts">
	import RouteComparator from '$components/RouteComparator.svelte';
	import CorridorMap from '$components/CorridorMap.svelte';
	import {
		activeConvoy,
		routeCandidates,
		selectedRouteId,
		setRoutes,
		selectRoute,
	} from '$stores/convoy';
	import { createMovement, planMovement } from '$api/client';
	import type { VvipClass } from '$lib/types';

	let vvipClass = $state<VvipClass>('Y');
	let originText = $state('28.6139, 77.2090');
	let destinationText = $state('28.6350, 77.2250');
	let departureTime = $state('');
	let isComputing = $state(false);
	let errorMsg = $state('');

	function parseCoord(text: string): [number, number] | null {
		const parts = text.split(',').map((s) => parseFloat(s.trim()));
		if (parts.length === 2 && parts.every((p) => !isNaN(p))) {
			// Input: "lat, lon" — convert to [lon, lat]
			return [parts[1], parts[0]];
		}
		return null;
	}

	async function handleSubmit(e: Event) {
		e.preventDefault();
		errorMsg = '';

		const origin = parseCoord(originText);
		const destination = parseCoord(destinationText);

		if (!origin || !destination) {
			errorMsg = 'Invalid coordinates. Use format: lat, lon';
			return;
		}

		isComputing = true;
		try {
			const { movementId } = await createMovement({
				origin,
				destination,
				vvipClass,
				plannedDeparture: departureTime || new Date().toISOString(),
			});

			const result = await planMovement(movementId, {
				origin,
				destination,
				vvipClass,
				plannedDeparture: departureTime || new Date().toISOString(),
			});

			setRoutes(result.routes);
		} catch (err) {
			errorMsg = err instanceof Error ? err.message : 'Failed to compute routes';
		} finally {
			isComputing = false;
		}
	}

	function handleRouteSelect(routeId: string) {
		selectRoute(routeId);
	}
</script>

<div class="planning-page">
	<div class="planning-main">
		<h1>Route Planning</h1>

		<form class="movement-form" onsubmit={handleSubmit}>
			<fieldset>
				<legend>New Movement</legend>

				<div class="form-row">
					<label>
						VVIP Class
						<select bind:value={vvipClass}>
							<option value="Z+">Z+ — PM, President (6-lane min, full closure)</option>
							<option value="Z">Z — Cabinet, Judiciary (4-lane min, partial closure)</option>
							<option value="Y">Y — State Ministers (2-lane min, speed restriction)</option>
							<option value="X">X — Standard VIP (signal priority only)</option>
						</select>
					</label>

					<label>
						Planned Departure
						<input type="datetime-local" bind:value={departureTime} />
					</label>
				</div>

				<div class="form-row">
					<label>
						Origin (lat, lon)
						<input type="text" bind:value={originText} placeholder="28.6139, 77.2090" />
					</label>

					<label>
						Destination (lat, lon)
						<input type="text" bind:value={destinationText} placeholder="28.6350, 77.2250" />
					</label>
				</div>

				{#if errorMsg}
					<div class="error-msg">{errorMsg}</div>
				{/if}

				<button type="submit" disabled={isComputing}>
					{#if isComputing}
						Computing...
					{:else}
						Compute Routes
					{/if}
				</button>
			</fieldset>
		</form>

		<RouteComparator
			routes={$routeCandidates}
			selectedRouteId={$selectedRouteId}
			onSelect={handleRouteSelect}
		/>

		{#if $routeCandidates.length > 0}
			<div class="route-actions">
				<p class="recommendation">
					Recommended: Route #{$routeCandidates.findIndex(r => r.routeId === $selectedRouteId) + 1}
					— composite score {$routeCandidates.find(r => r.routeId === $selectedRouteId)?.compositeScore.toFixed(1)}
				</p>
				<a href="/command" class="btn-proceed">Proceed to Command Center</a>
			</div>
		{/if}
	</div>

	<div class="planning-map">
		<CorridorMap zoom={13} />
	</div>
</div>

<style>
	.planning-page {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 1.5rem;
		flex: 1;
		min-height: 0;
	}

	.planning-main {
		overflow-y: auto;
		padding-right: 0.5rem;
	}

	.planning-main h1 { margin-bottom: 1.25rem; }

	.planning-map {
		border-radius: 0.5rem;
		overflow: hidden;
	}

	.movement-form fieldset {
		border: 1px solid #334155;
		border-radius: 0.5rem;
		padding: 1.25rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.movement-form legend {
		color: #94a3b8;
		font-size: 0.85rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.form-row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.75rem;
	}

	.movement-form label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		font-size: 0.825rem;
		color: #94a3b8;
	}

	.movement-form input,
	.movement-form select {
		padding: 0.5rem;
		border: 1px solid #334155;
		border-radius: 0.25rem;
		background: #0f172a;
		color: #f1f5f9;
		font-size: 0.875rem;
	}

	.movement-form button {
		padding: 0.6rem 1.5rem;
		background: #3b82f6;
		color: white;
		border: none;
		border-radius: 0.375rem;
		cursor: pointer;
		font-weight: 600;
		align-self: flex-start;
		font-size: 0.9rem;
	}

	.movement-form button:hover:not(:disabled) { background: #2563eb; }
	.movement-form button:disabled { opacity: 0.5; cursor: not-allowed; }

	.error-msg {
		padding: 0.5rem 0.75rem;
		background: #450a0a;
		border: 1px solid #991b1b;
		border-radius: 0.25rem;
		color: #fca5a5;
		font-size: 0.85rem;
	}

	.route-actions {
		margin-top: 1rem;
		padding: 1rem;
		background: #1e293b;
		border: 1px solid #334155;
		border-radius: 0.5rem;
	}

	.recommendation {
		color: #94a3b8;
		font-size: 0.875rem;
		margin-bottom: 0.75rem;
	}

	.btn-proceed {
		display: inline-block;
		padding: 0.5rem 1.25rem;
		background: #22c55e;
		color: #0f172a;
		text-decoration: none;
		border-radius: 0.375rem;
		font-weight: 600;
		font-size: 0.875rem;
	}

	.btn-proceed:hover { background: #16a34a; }
</style>
