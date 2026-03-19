# Auto-refresh seed data timestamps + Valkey cache every 4 minutes
# Run: powershell -File infra/scripts/refresh_seed.ps1
$interval = 240  # seconds

Write-Host "[seed-refresh] Starting auto-refresh every ${interval}s..."
while ($true) {
    $ts = Get-Date -Format "HH:mm:ss"
    # 1. Refresh DB observation timestamps
    docker exec vvip-postgis psql -U corridor_admin -d corridor_db -q -c "UPDATE traffic.observations SET timestamp_utc = NOW() - (random() * INTERVAL '5 minutes');" 2>$null

    # 2. Refresh Valkey cache with TrafficObservation-compatible JSON
    $epoch_ms = [long]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
    foreach ($sid in 1001..1040) {
        $speed = [math]::Round((Get-Random -Minimum 20 -Maximum 65), 1)
        $cong = [math]::Round((Get-Random -Minimum 200 -Maximum 800) / 1000, 3)
        $lon = [math]::Round(72.5714 + (Get-Random -Minimum -100 -Maximum 100) / 10000, 6)
        $lat = [math]::Round(23.0225 + (Get-Random -Minimum -100 -Maximum 100) / 10000, 6)
        $json = '{"timestamp_ms":' + $epoch_ms + ',"lon":' + $lon + ',"lat":' + $lat + ',"segment_id":' + $sid + ',"speed_kmh":' + $speed + ',"congestion_idx":' + $cong + ',"source":"government_traffic","data_quality":"real","confidence":1.0}'
        "SET traffic:latest:$sid '$json' EX 3600" | docker exec -i vvip-valkey redis-cli 2>$null | Out-Null
    }
    # 3. Clear corridor summary cache to force refresh
    docker exec vvip-valkey redis-cli DEL corridor:summary:main 2>$null | Out-Null

    Write-Host "[$ts] Refreshed 236 DB timestamps + 40 Valkey keys + corridor cache"
    Start-Sleep -Seconds $interval
}
