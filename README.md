# Dotfiles

Minhas configurações para Windows: Komorebi, Yasb, Terminal e Utilitários.

## Índice

- [Pré-requisitos](#pré-requisitos)
- [Python](#python)
- [Node.js](#nodejs)
- [Starship](#starship)
- [Yazi](#yazi)
- [YASB](#yasb)
- [Tacky-Borders](#tacky-borders)
- [Lightshot](#lightshot)
- [Komorebi (fork)](#komorebi-fork)
- [Aplicando as configurações](#aplicando-as-configurações)

---

## Pré-requisitos

Todos os comandos abaixo são para o **PowerShell** e usam o [winget](https://learn.microsoft.com/windows/package-manager/winget/), que já vem no Windows 10/11 atualizado.

Instale o Git antes de começar:

```powershell
winget install --id Git.Git -e
```

> Depois de instalar qualquer programa, feche e abra o terminal para o `PATH` ser atualizado.

---

## Python

```powershell
winget install --id Python.Python.3.13 -e
```

Confira:

```powershell
python --version
pip --version
```

---

## Node.js

Versão LTS:

```powershell
winget install --id OpenJS.NodeJS.LTS -e
```

Confira:

```powershell
node --version
npm --version
```

---

## Starship

Prompt multiplataforma ([starship.rs](https://starship.rs)).

```powershell
winget install --id Starship.Starship -e
```

Ative no PowerShell adicionando ao final do `$PROFILE`:

```powershell
# Abre o profile no Notepad (cria o arquivo se não existir)
if (!(Test-Path $PROFILE)) { New-Item -ItemType File -Path $PROFILE -Force }
notepad $PROFILE
```

```powershell
Invoke-Expression (&starship init powershell)
```

A configuração fica em `~/.config/starship.toml`. Para os ícones aparecerem corretamente, use uma [Nerd Font](https://www.nerdfonts.com/) no terminal.

---

## Yazi

Gerenciador de arquivos para o terminal ([yazi-rs.github.io](https://yazi-rs.github.io)).

```powershell
winget install --id sxyazi.yazi -e
```

Dependências opcionais (pré-visualização, busca, etc.):

```powershell
winget install --id Gyan.FFmpeg -e
winget install --id 7zip.7zip -e
winget install --id jqlang.jq -e
winget install --id sharkdp.fd -e
winget install --id BurntSushi.ripgrep.MSVC -e
winget install --id junegunn.fzf -e
winget install --id ajeetdsouza.zoxide -e
winget install --id ImageMagick.ImageMagick -e
```

O Yazi usa o `file.exe` que vem com o Git para Windows. Defina a variável de ambiente (uma vez):

```powershell
[Environment]::SetEnvironmentVariable("YAZI_FILE_ONE", "C:\Program Files\Git\usr\bin\file.exe", "User")
```

Para abrir:

```powershell
yazi
```

A configuração fica em `%APPDATA%\yazi\config`.

---

## YASB

Barra de status para Windows ([github.com/amnweb/yasb](https://github.com/amnweb/yasb)).

```powershell
winget install --id AmN.yasb -e
```

A configuração fica em `%USERPROFILE%\.config\yasb` (`config.yaml` e `styles.css`).

---

## Tacky-Borders

Bordas customizáveis para janelas ([github.com/lukeyou05/tacky-borders](https://github.com/lukeyou05/tacky-borders)).

### Opção 1: release pronta (recomendado)

1. Baixe o `.zip` mais recente em <https://github.com/lukeyou05/tacky-borders/releases>.
2. Extraia em uma pasta fixa, por exemplo `C:\Tools\tacky-borders`.
3. Execute o `tacky-borders.exe`. Na primeira execução ele gera a config em `%USERPROFILE%\.config\tacky-borders\`.
4. Para iniciar com o Windows, ative o auto-start pelo ícone na bandeja do sistema.

### Opção 2: compilar do código-fonte

Precisa do [Rust](https://rustup.rs) e do Visual Studio Build Tools (carga de trabalho "Desenvolvimento para desktop com C++"). Veja a seção do [Komorebi](#komorebi-fork) para instalar as duas coisas.

```powershell
git clone https://github.com/lukeyou05/tacky-borders.git
cd tacky-borders
cargo build --release
```

O executável fica em `target\release\tacky-borders.exe`.

---

## Lightshot

Ferramenta de captura de tela.

```powershell
winget install --id Skillbrains.Lightshot -e
```

Se o ID não for encontrado, procure o nome atual com `winget search lightshot`.

---

## Komorebi (fork)

Eu uso o fork **[cipheroute/komorebi](https://github.com/cipheroute/komorebi/tree/first-simple)**, branch **`first-simple`**, e não a versão oficial. Por isso a instalação é feita **compilando do código-fonte**, e não pelo `winget`.

### 1. Remova o Komorebi oficial (se estiver instalado)

```powershell
komorebic stop --whkd
winget uninstall --id LGUG2Z.komorebi
```

Se algum comando der erro por não estar instalado, pode ignorar.

### 2. Instale as ferramentas de compilação

```powershell
winget install --id Rustlang.Rustup -e
winget install --id Microsoft.VisualStudio.2022.BuildTools -e --override "--passive --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

Feche e abra o terminal e confira:

```powershell
rustc --version
cargo --version
```

### 3. Clone o fork na branch `first-simple`

```powershell
git clone --branch first-simple https://github.com/cipheroute/komorebi.git
cd komorebi
```

Confirme que está na branch certa:

```powershell
git branch --show-current
```

### 4. Compile e instale

```powershell
cargo install --path komorebi --locked
cargo install --path komorebic --locked
```

Se você usa a barra do Komorebi (opcional):

```powershell
cargo install --path komorebi-bar --locked
```

Os binários são instalados em `%USERPROFILE%\.cargo\bin`, que o Rustup já adiciona ao `PATH`.

### 5. Inicie

```powershell
komorebic start
```

Para iniciar automaticamente com o Windows:

```powershell
komorebic enable-autostart
```

### Atualizando o fork

```powershell
cd komorebi
komorebic stop
git pull origin first-simple
cargo install --path komorebi --locked
cargo install --path komorebic --locked
komorebic start
```

A configuração principal é o `komorebi.json`, em `%USERPROFILE%\komorebi.json`.

---

## Aplicando as configurações

Depois de instalar tudo, copie os arquivos deste repositório para os locais certos. Ajuste os caminhos de origem conforme a estrutura das pastas do repo.

```powershell
git clone https://github.com/danteshoww/dotfiles "$HOME\dotfiles"
cd "$HOME\dotfiles"

# Komorebi
Copy-Item .\komorebi\komorebi.json "$HOME\komorebi.json" -Force

# YASB
Copy-Item .\yasb\* "$HOME\.config\yasb\" -Recurse -Force

# Tacky-Borders
Copy-Item .\tacky-borders\* "$HOME\.config\tacky-borders\" -Recurse -Force

# Starship
Copy-Item .\starship\starship.toml "$HOME\.config\starship.toml" -Force

# Yazi
Copy-Item .\yazi\* "$env:APPDATA\yazi\config\" -Recurse -Force
```

Se preferir que o repositório seja a fonte única dos arquivos, use links simbólicos (PowerShell como administrador):

```powershell
New-Item -ItemType SymbolicLink -Path "$HOME\.config\yasb" -Target "$HOME\dotfiles\yasb"
```

Para aplicar as mudanças, reinicie os programas ou rode `komorebic stop` e `komorebic start`.
