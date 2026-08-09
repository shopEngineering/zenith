# ZENITH/OS — Windows first-time setup (run in PowerShell as Administrator)
# One-time: installs WSL + Ubuntu. After the reboot it prompts for, finish inside
# Ubuntu with the 3 lines this script prints. (Claude Code runs best in WSL.)

Write-Host ""
Write-Host "  ZENITH/OS — Windows setup" -ForegroundColor Cyan
Write-Host ""

# Is WSL already present?
$wsl = Get-Command wsl -ErrorAction SilentlyContinue
if (-not $wsl) {
    Write-Host "  Installing WSL + Ubuntu (a reboot will be required)..." -ForegroundColor Yellow
    wsl --install -d Ubuntu
    Write-Host ""
    Write-Host "  >> REBOOT, then reopen this and run it again to see the next steps." -ForegroundColor Yellow
    exit
}

# WSL present — check for an Ubuntu distro
$distros = (wsl --list --quiet) -join " "
if ($distros -notmatch "Ubuntu") {
    Write-Host "  Installing Ubuntu..." -ForegroundColor Yellow
    wsl --install -d Ubuntu
    Write-Host "  >> Finish Ubuntu's first-run setup (username/password), then continue below." -ForegroundColor Yellow
}

Write-Host "  WSL is ready. Finish inside Ubuntu — open 'Ubuntu' from Start, then run:" -ForegroundColor Green
Write-Host ""
Write-Host '    sudo apt update && sudo apt install -y python3 tmux git' -ForegroundColor White
Write-Host '    # install Claude Code so `claude` works (see claude.com/claude-code), then:' -ForegroundColor DarkGray
Write-Host '    git clone https://github.com/shopEngineering/zenith.git && cd zenith-os && ./install.sh' -ForegroundColor White
Write-Host ""
Write-Host "  After that, just type  " -NoNewline; Write-Host "zenith" -ForegroundColor Cyan -NoNewline
Write-Host "  in Ubuntu to launch it in your Windows browser."
Write-Host ""
