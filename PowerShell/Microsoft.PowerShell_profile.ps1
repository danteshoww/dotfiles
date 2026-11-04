Clear-Host
cls

## StarShip Shell
Invoke-Expression (&starship init powershell)
Import-Module $HOME\.config\winwal.psm1
Import-Module $HOME\.config\screenshot.ps1

# WINWAL

function pywal_to_starship {
	Update-WalTerminal *>&1;
	bash ~/winwaltostarship.sh;
	bash ~/waltostarship.sh;
}

function update_pywal_to_starship{
	Update-WalTheme *>&1;
	bash ~/winwaltostarship.sh;
	bash ~/waltostarship.sh;
}

function screenshot_to_pywal_to_starship{
	Get-Wallpaper;
	pywal_to_starship;
}

Set-Alias -Name Pywal-Starship -Value pywal_to_starship
Set-Alias -Name Wallpaper-Starship -Value update_pywal_to_starship
Set-Alias -Name Screenshot-Starship -Value screenshot_to_pywal_to_starship

#Import-Module "C:\Users\Danteshow\winwal\winwal.psm1"

# Icons
Import-Module -Name Terminal-Icons

# PSReadLine
Set-PSReadLineOption -BellStyle None
Set-PSReadLineOption -PredictionSource History
Set-PSReadLineOption -PredictionViewStyle ListView

# FastFetch need to work

# Prompt

#Import-Module posh-git
#oh-my-posh init pwsh --config "jandedobbeleer" | Invoke-Expression


# Alias
Set-Alias g git
Set-Alias ll ls
Set-Alias tig 'C:\Program Files\Git\usr\bin\tig.exe'
Set-Alias less 'C:\Program Files\Git\usr\bin\less.exe'

function ytv {
    # Baixa vídeo (máx 1080p), incorpora a capa, limpa o nome do arquivo e numera se for playlist
    yt-dlp -f "bestvideo[height<=1080]+bestaudio/best[height<=1080]" `
           -P "C:\Users\Danteshow\Videos\YT-DLP" `
           -o "%(playlist_index&{} - |)s%(title)s.%(ext)s" `
           --embed-thumbnail `
           --restrict-filenames `
           --no-write-subs --no-embed-subs  `
           $args
}

function ytm {
    # Extrai áudio em MP3, incorpora a capa, limpa o nome do arquivo e numera se for playlist
    yt-dlp -x --audio-format mp3 `
           -P "C:\Users\Danteshow\Music\YT-DLP" `
           -o "%(playlist_index&{} - |)s%(title)s.%(ext)s" `
           --embed-thumbnail `
           --restrict-filenames `
           --no-write-subs --no-embed-subs  `
           $args
}

#$eagleFastfetchRoot = 'C:\Users\Danteshow\.config\fastfetch'
#$eagleFastfetchConfig = Join-Path $eagleFastfetchRoot 'config.jsonc'

#if ((Get-Command fastfetch -ErrorAction SilentlyContinue) -and
 #   (Test-Path -LiteralPath $eagleFastfetchConfig)) {
  #  $env:FASTFETCH_EAGLE_ROOT = $eagleFastfetchRoot
  #  fastfetch --config $eagleFastfetchConfig
#}

function Mix-Audio {
    param(
        [Parameter(Mandatory = $true)]
        [string]$InputFile
    )

    $InputFile = (Resolve-Path $InputFile).Path

    $directory = Split-Path $InputFile -Parent
    $filename = [System.IO.Path]::GetFileNameWithoutExtension($InputFile)

    $outputFile = Join-Path $directory "${filename}_mixada.mp4"

    ffmpeg -i "$InputFile" `
        -filter_complex "[0:a:0][0:a:1][0:a:2][0:a:3]amix=inputs=4:duration=longest[aout]" `
        -map 0:v `
        -map "[aout]" `
        -c:v copy `
        -c:a aac `
        -b:a 192k `
        -movflags +faststart `
        "$outputFile"

    if ($LASTEXITCODE -eq 0) {
        Write-Host "`nArquivo criado:" -ForegroundColor Green
        Write-Host "$outputFile" -ForegroundColor Cyan
    }
}

Set-Alias mixaudio Mix-Audio
# Import the Chocolatey Profile that contains the necessary code to enable
# tab-completions to function for `choco`.
# Be aware that if you are missing these lines from your profile, tab completion
# for `choco` will not function.
# See https://ch0.co/tab-completion for details.
$ChocolateyProfile = "$env:ChocolateyInstall\helpers\chocolateyProfile.psm1"
if (Test-Path($ChocolateyProfile)) {
  Import-Module "$ChocolateyProfile"
}

function claude-free {
    $env:ANTHROPIC_BASE_URL = "http://127.0.0.1:31415"
    $env:ANTHROPIC_API_KEY = "freellmapi-42388f6ac2c1206be51947dbc5bfc3dd7ef34c8573294fe6"
    claude @args
}

