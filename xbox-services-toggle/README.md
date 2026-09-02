# gaming-services-toggle

Installs Microsoft Gaming Services + Xbox Identity Provider right before a Forza Horizon session, then fully removes both (packages and leftover service registry keys) once you're done — instead of leaving Xbox packages and their two background services (`GamingServices`, `GamingServicesNet`) installed all the time.

Written to fix "Invalid Gaming Services Detected" on Forza Horizon 6 when those packages have been stripped from Windows (e.g. by a debloat script).

## Files

- `GamingServicesToggle.bat` — self-elevating menu. Double-click, approve the UAC prompt.
- `Invoke-GamingServicesAction.ps1` — does the actual work. Called by the `.bat`, or run directly from an elevated PowerShell:
```powershell
  powershell -ExecutionPolicy Bypass -File .\Invoke-GamingServicesAction.ps1 -Action Install
    powershell -ExecutionPolicy Bypass -File .\Invoke-GamingServicesAction.ps1 -Action Remove
      powershell -ExecutionPolicy Bypass -File .\Invoke-GamingServicesAction.ps1 -Action Status
      ```

      Keep both files in the same folder — the `.bat` looks for the `.ps1` right next to it.

      ## Usage

      1. Run `GamingServicesToggle.bat` before a session, choose **[1] Install**.
      2. Play. If FH6 still throws the error on the very first launch, reboot once — that alone clears it for most people.
      3. When done, run it again and choose **[2] Remove**.

      **[3] Status** shows what's currently installed/registered without changing anything.

      ## What Install does

      - Installs `Microsoft.GamingServices` and `Microsoft.XboxIdentityProvider` — tries `winget install --source msstore --silent` first, falls back to opening the Store page and polling until the install actually finishes.
      - Warns if the installed Gaming Services build is older than `37.114.10001.0`, Forza Horizon 6's published minimum.

      ## What Remove does

      - Uninstalls both packages (`Remove-AppxPackage -AllUsers`).
      - Stops and deletes the `GamingServices` / `GamingServicesNet` Windows services properly (`Stop-Service` + `sc.exe delete`) before clearing their leftover registry keys under `HKLM:\SYSTEM\CurrentControlSet\Services\`, instead of ripping the registry key out from under a service the SCM still thinks is registered.

      ## Notes

      - Needs admin — the `.bat` self-elevates via UAC.
      - Manual, not scheduled — unlike `alamo-drafthouse/`, run this by hand before/after each session.
      - If this stops being enough on its own, Microsoft's official Gaming Services Repair Tool (`winget install --id Microsoft.Gaming.GamingServicesRepairTool`) also covers the Xbox App and Game Bar — heavier than this script's minimal footprint, so it's not automated here.
