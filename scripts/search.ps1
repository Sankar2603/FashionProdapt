# Search through the gateway.
# Usage:  .\scripts\search.ps1 'camisa de lino para el verano por menos de $40'
param([Parameter(Mandatory = $true)][string]$Query, [int]$TopN = 5)

$body = @{ query = $Query; top_n = $TopN } | ConvertTo-Json
$r = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/search" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($body))

Write-Host "`nCorrelation ID: $($r.correlation_id)"
Write-Host "Intent: '$($r.intent.search_query)'  max_price=$($r.intent.max_price)  language=$($r.intent.language)  source=$($r.intent.source)"
if ($r.degraded.Count -gt 0) { Write-Host "Degraded: $($r.degraded -join ', ')" -ForegroundColor Yellow }
$r.results | Select-Object relevance, price, title | Format-Table -AutoSize
$r.latency_ms