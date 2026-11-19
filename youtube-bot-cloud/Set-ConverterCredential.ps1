$ErrorActionPreference = 'Stop'
$cloud = Read-Host 'Telegram Serverless CLI access token from BotFather' -AsSecureString
$key = Read-Host 'New Render CONVERTER_API_KEY' -AsSecureString
try {
    $token = [Net.NetworkCredential]::new('', $cloud).Password
    $value = [Net.NetworkCredential]::new('', $key).Password
    if ($token -notmatch '^(app\d+):[^:\s]+$') { throw 'Invalid CLI access token format.' }
    $appId = $Matches[1]
    if ($value -notmatch '^[A-Za-z0-9_-]{32,256}$') {
        throw 'Use a random Render key with at least 32 URL-safe characters.'
    }

    # A private management invocation. This module is never deployed.
    $module = @"
import { db } from 'sdk';
export default async function ({ key }) {
    await db.run(
        "INSERT INTO converter_settings(name, value) VALUES ('api_key', :key) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
        { ':key': key }
    );
    return true;
}
"@
    $body = @{
        module = 'handlers/provision'
        sources = @{ 'handlers/provision' = $module }
        args = @{ key = $value }
        ctx = @{}
    } | ConvertTo-Json -Depth 5 -Compress
    try {
        $result = Invoke-RestMethod -Method Post -Uri "https://cloud.telegram.org/$appId/manage/run" `
            -Headers @{ Authorization = "Bearer $token" } -ContentType 'application/json' `
            -Body $body -MaximumRedirection 0 -TimeoutSec 30
    } catch {
        throw 'Provisioning failed. Verify the CLI token, database migration, and network connection.'
    }
    if (-not $result.ok) { throw 'Telegram rejected provisioning. Verify the database migration.' }
    Write-Output 'Telegram Serverless credential stored.'
} finally {
    Remove-Variable token,value,body -ErrorAction SilentlyContinue
    $cloud.Dispose()
    $key.Dispose()
}
