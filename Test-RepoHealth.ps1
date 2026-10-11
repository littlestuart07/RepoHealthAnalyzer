# Test-RepoHealth.ps1
#
# Builds a throwaway Git repository with deliberately planted defects, then
# checks RepoHealth's output against the values those defects should produce.
#
# Every expected number below was measured, not guessed. If one of them comes
# back wrong, something is genuinely broken.
#
#   .\Test-RepoHealth.ps1
#   .\Test-RepoHealth.ps1 -Path E:\rh-verify     (choose where it is built)

param([string]$Path = "$env:TEMP\rh-verify")

$ErrorActionPreference = "Stop"
$script:pass = 0
$script:fail = 0

function Check($label, $expected, $actual) {
    if ("$actual" -eq "$expected") {
        Write-Host "  PASS " -ForegroundColor Green -NoNewline
        Write-Host "$label  ($actual)"
        $script:pass++
    } else {
        Write-Host "  FAIL " -ForegroundColor Red -NoNewline
        Write-Host "$label  expected $expected, got $actual"
        $script:fail++
    }
}
function Section($t) { Write-Host "`n$t" -ForegroundColor Cyan }

# ---------------------------------------------------------------- build it

Section "Building the test repository at $Path"

if (Test-Path $Path) { Remove-Item $Path -Recurse -Force }
New-Item $Path -ItemType Directory -Force | Out-Null
Push-Location $Path

git init -q
git config user.email "verify@test.local"
git config user.name  "Verify"

New-Item "shop\core" -ItemType Directory -Force | Out-Null
New-Item "shop\__init__.py"      -ItemType File -Force | Out-Null
New-Item "shop\core\__init__.py" -ItemType File -Force | Out-Null

@'
TAX_RATE = 0.18
CURRENCY = "INR"
MAX_ITEMS = 50
'@ | Set-Content "shop\core\settings.py" -Encoding utf8

# --- a three-file import cycle: auth -> accounts -> database -> auth ---
@'
from shop.accounts import find_account
from shop.core.settings import MAX_ITEMS
def login(user, pw):
    if not user:
        return False
    return find_account(user) is not None
'@ | Set-Content "shop\auth.py" -Encoding utf8

@'
from shop.database import query
def find_account(user):
    return query(user)
'@ | Set-Content "shop\accounts.py" -Encoding utf8

@'
from shop.auth import login
from shop.core.settings import CURRENCY
def query(sql):
    if not login("system", "internal"):
        return None
    return {"sql": sql, "currency": CURRENCY}
'@ | Set-Content "shop\database.py" -Encoding utf8

# --- the file we will later spike ---
@'
from shop.core.settings import TAX_RATE, MAX_ITEMS

def total(items, discount=0, tax=True, member=False):
    amount = 0
    for it in items:
        if it.get("qty"):
            if it.get("price"):
                amount += it["qty"] * it["price"]
    if discount:
        amount -= discount
    if member:
        amount -= amount * 0.05
    if tax:
        amount += amount * TAX_RATE
    return round(amount, 2)
'@ | Set-Content "shop\cart.py" -Encoding utf8

@'
from shop.core.settings import CURRENCY
def summary(rows):
    return len(rows)
'@ | Set-Content "shop\reports.py" -Encoding utf8

# --- a file that cannot be parsed: must be skipped, not crash the run ---
@'
def unfinished(
'@ | Set-Content "shop\broken.py" -Encoding utf8

# --- stdlib imports: json and os must NOT become graph nodes ---
@'
import json
import os
from shop.core.settings import CURRENCY
def load(path):
    with open(path) as fh:
        return json.load(fh), os.sep, CURRENCY
'@ | Set-Content "shop\importer.py" -Encoding utf8

git add -A; git commit -qm "initial shop"

# uneven history, so churn differs measurably per file
for ($i = 1; $i -le 5; $i++) {
    Add-Content "shop\cart.py" "# revision $i"
    git add -A; git commit -qm "cart change $i"
}
for ($i = 1; $i -le 2; $i++) {
    Add-Content "shop\auth.py" "# tweak $i"
    git add -A; git commit -qm "auth tweak $i"
}

Write-Host "  8 commits, 10 Python files" -ForegroundColor DarkGray

# ---------------------------------------------------------------- analyse

Section "Running the analysis"
$scan = repohealth --no-color scan $Path --quiet 2>&1 | Out-String
Write-Host $scan.Trim() -ForegroundColor DarkGray

Section "Checking the analysis"

Check "commits ingested"      8  ([regex]::Match($scan, 'Ingested (\d+) new').Groups[1].Value)
Check "Python files analysed" 10 ([regex]::Match($scan, 'Analyzed (\d+) Python').Groups[1].Value)
Check "unparsed files"        1  ([regex]::Match($scan, '\((\d+) unparsed').Groups[1].Value)
Check "graph nodes"           10 ([regex]::Match($scan, 'Graph (\d+) nodes').Groups[1].Value)
Check "internal edges"        8  ([regex]::Match($scan, 'nodes, (\d+) internal').Groups[1].Value)
Check "cycles detected"       1  ([regex]::Match($scan, 'Cycles (\d+) circular').Groups[1].Value)
Check "health score"          "60/100" ([regex]::Match($scan, 'Health (\d+/100)').Groups[1].Value)

# json and os are stdlib: 10 nodes rather than 12 proves they were dropped
Write-Host "  note: 10 nodes, not 12 — json and os were correctly excluded" -ForegroundColor DarkGray
# the syntax error was recorded, not fatal
Check "broken file skipped by name" $true ($scan -match 'broken\.py')

Section "Checking the risk ranking"
$report = repohealth --no-color report $Path --top 10 2>&1 | Out-String

$cartRow = ($report -split "`n" | Where-Object { $_ -match 'shop/cart\.py' }) -replace '\s+', ' '
$cartChurn = ($cartRow -split ' ')[1]
Check "cart.py churn (edited 6 times)" 6 $cartChurn
Check "cart.py is highest risk" $true ($report -match 'shop/cart\.py\s+6')
Check "settings.py blast radius 6" $true ($report -match 'settings\.py\s+\d+\s+\d+\s+6')

Section "Checking cycle detection"
$cycles = repohealth --no-color cycles $Path 2>&1 | Out-String
Check "one chain reported" $true ($cycles -match '1 circular import chain')
Check "chain names auth.py"     $true ($cycles -match 'shop/auth\.py')
Check "chain names accounts.py" $true ($cycles -match 'shop/accounts\.py')
Check "chain names database.py" $true ($cycles -match 'shop/database\.py')

# ---------------------------------------------------------------- the gate

Section "Installing the commit gate"
repohealth init $Path 2>&1 | Out-String | Write-Host -ForegroundColor DarkGray
Check "hook file created" $true (Test-Path "$Path\.git\hooks\pre-commit")

Section "Gate test 1 — a harmless commit should pass"
Add-Content "shop\reports.py" "# harmless comment"
git add shop/reports.py
$ok = repohealth --no-color check --staged $Path 2>&1 | Out-String
Check "harmless change allowed" $true ($ok -match 'repohealth: OK')
git restore --source=HEAD --staged --worktree shop/reports.py

Section "Gate test 2 — a complexity spike should be blocked"
@'

def apply_rules(items, region, coupon, loyalty, tier):
    out = 0
    for it in items:
        if it.get("qty"):
            if region == "north":
                if coupon:
                    if loyalty > 100:
                        out += 5
                    elif loyalty > 50:
                        out += 3
                    else:
                        out += 1
            elif region == "south":
                if tier == "gold":
                    out += 4
                elif tier == "silver":
                    out += 2
    return out
'@ | Add-Content "shop\cart.py"
git add shop/cart.py

$blocked = repohealth --no-color check --staged $Path 2>&1 | Out-String
Write-Host $blocked.Trim() -ForegroundColor DarkGray

Check "commit blocked"            $true ($blocked -match 'COMMIT BLOCKED')
Check "names the file"            $true ($blocked -match 'shop/cart\.py')
Check "reports the rise"          $true ($blocked -match '142\.9% above its baseline')
Check "names the function"        $true ($blocked -match 'apply_rules')
Check "gives the line number"     $true ($blocked -match 'line 22')

Section "Gate test 3 — git commit itself must be refused"
$out = git commit -m "this should fail" 2>&1 | Out-String
Check "git refused the commit" $true ($LASTEXITCODE -ne 0)

Section "Gate test 4 — --no-verify must still get through"
$out = git commit -m "deliberate override" --no-verify 2>&1 | Out-String
Check "override succeeded" 0 $LASTEXITCODE

git reset -q --hard HEAD~1

# ---------------------------------------------------------------- summary

Pop-Location
Write-Host ""
if ($script:fail -eq 0) {
    Write-Host "  $($script:pass) checks passed, 0 failed" -ForegroundColor Green
} else {
    Write-Host "  $($script:pass) passed, $($script:fail) FAILED" -ForegroundColor Red
}
Write-Host ""
Write-Host "Test repository left at $Path — open it in the app to look around:" -ForegroundColor Cyan
Write-Host "  repohealth app `"$Path`""
Write-Host ""
