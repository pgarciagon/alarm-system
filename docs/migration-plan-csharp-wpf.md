# Plan: Migración Alarm System de Python a C# / .NET 8 (WPF)

## Contexto

La aplicación Alarm System (~6,000 líneas Python, 16 archivos) se usa en consultas médicas con datos sensibles. Actualmente depende de Python + PyInstaller, lo que causa problemas en Windows Server 2022 (bugs de subprocess, hasattr, permisos). Migrar a C# nativo elimina la dependencia de Python, reduce el tamaño del exe, mejora la estabilidad y aprovecha las APIs nativas de Windows.

## Estructura de la Solución

```
AlarmSystem.sln
├── src/
│   ├── AlarmSystem.Common/           (Class Library)
│   │   ├── Protocol.cs               - 10 msg types + JSON
│   │   ├── Config.cs                 - ServerConfig/ClientConfig, JSON r/w
│   │   ├── AutoStart.cs              - schtasks
│   │   ├── Discovery.cs              - Subnet scanner
│   │   └── Version.cs
│   ├── AlarmSystem.Server/           (WPF exe)
│   │   ├── Services/                 - WebSocket hub (Fleck), ClientRegistry, HealthMonitor
│   │   ├── ViewModels/               - DashboardVM, ClientRowVM
│   │   ├── Views/                    - Dashboard, EditHotkey, EditRoomName
│   │   └── TrayIcon/
│   ├── AlarmSystem.Client/           (WPF exe)
│   │   ├── Services/                 - WebSocket client, HotkeyService, SoundService
│   │   ├── ViewModels/               - StatusVM, SettingsVM, AlarmOverlayVM
│   │   ├── Views/                    - Status, AlarmOverlay, Settings, Banner, ScanServer
│   │   └── TrayIcon/
│   └── AlarmSystem.Installer/        (WPF exe)
│       ├── Views/InstallerWindow.xaml
│       └── Services/InstallerService.cs
└── tests/                            (xUnit)
```

## Dependencias NuGet

| Paquete | Reemplaza | Propósito |
|---------|-----------|-----------|
| **Fleck** | websockets (server) | WebSocket server ligero |
| **System.Text.Json** (built-in) | json + tomli | Config + protocol |
| **CommunityToolkit.Mvvm** | N/A | MVVM source generators |
| **NAudio** | pygame.mixer | WAV playback con looping |
| **H.NotifyIcon.Wpf** | pystray + Pillow | System tray nativo |
| **P/Invoke RegisterHotKey** | keyboard lib | Global hotkey (0 deps) |
| **xUnit + Moq** | pytest | Testing |

## Decisiones Clave

- **Config**: JSON (migrar de TOML) — System.Text.Json built-in
- **WebSocket server**: Fleck — ligero, sin ASP.NET overhead
- **Global hotkey**: RegisterHotKey WinAPI — 0 dependencias, integra con WPF
- **Sonido**: NAudio — sin conflictos con WPF
- **Deploy**: Self-contained single-file (`dotnet publish -r win-x64 --self-contained /p:PublishSingleFile=true`)
- **UI**: MVVM con CommunityToolkit

## Fases de Migración

### Fase 1: Common Library (1 semana)
- `Protocol.cs` — 10 message types como records, JSON con JsonPropertyName exacto para wire compatibility
- `Config.cs` — JSON read/write, auto-migración TOML→JSON en primer arranque
- `AutoStart.cs` — schtasks subprocess calls
- `Discovery.cs` — TcpClient async + SemaphoreSlim(64)
- Tests de compatibilidad con mensajes Python reales

### Fase 2: Server (1.5 semanas)
- `AlarmWebSocketServer.cs` — Fleck, ConcurrentDictionary, HealthMonitor
- `DashboardWindow.xaml` — Dark theme MVVM, DispatcherTimer 1.5s poll
- System tray H.NotifyIcon.Wpf
- **Validar**: C# server + Python clients existentes

### Fase 3: Client (2.5 semanas) — Más grande
- `AlarmWebSocketClient.cs` — Reconnect backoff [1,2,4,8,16,30]s
- `HotkeyService.cs` — RegisterHotKey P/Invoke (eliminar código macOS)
- `SoundService.cs` — NAudio WaveOutEvent looping
- 5 ventanas WPF: AlarmOverlay (fullscreen flash), Status, Banner, Settings, ScanServer
- `TrayIconManager.cs`
- **Validar**: C# client + Python server, y C# client + C# server

### Fase 4: Installer (1 semana)
- WPF installer: rol, probe red, config, file copy, schtasks, shortcuts, UAC, dev mode, uninstall, backup/rollback

### Fase 5: Integración (1 semana)
- Cross-compatibility C#↔Python
- Test LAN real multi-PC
- Dark theme polish
- Single-file publish + smoke test Windows limpio
- Soak test 24h+ (entorno médico)

## Estimación

| Componente | Python | C# est. | Tiempo |
|------------|--------|---------|--------|
| Common | 618 | ~800 | 1 sem |
| Server | 980 | ~1,300 | 1.5 sem |
| Client | 2,043 | ~2,350 | 2.5 sem |
| Installer | 1,823 | ~900 | 1 sem |
| Tests + integración | — | ~300 | 1.5 sem |
| **Total** | **~5,587** | **~5,650** | **~7-8 semanas** |

## Riesgos

**Alto**: Wire protocol incompatibility (mitigar con test fixtures del Python real), overlay fullscreen multi-monitor
**Medio**: Exe size ~60-80MB (probar PublishTrimmed), NAudio sin audio device en Server 2022
**Bajo**: schtasks (misma técnica), migración TOML→JSON

## Verificación
1. xUnit: roundtrip encode/decode con JSON capturado de Python
2. Server: C# server + Python clients → dashboard funcional
3. Client: C# client + Python server → alarma + overlay + sonido
4. Installer: Windows 10 limpio → autostart tras reboot
5. Estabilidad: 24h con 10+ clientes
