# Éléments cliquables visibles (UI Automation de Windows, sans installation) : fenêtre au premier plan + barre des
# tâches. Sortie : une ligne par élément, « type<TAB>nom<TAB>x<TAB>y<TAB>largeur<TAB>hauteur » en pixels physiques.
# Lecture seule : rien n'est cliqué ni modifié. Appelé par gateway/tools/pc.py.
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -Namespace LezardUi -Name Native -MemberDefinition @'
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(System.IntPtr value);
'@
[void][LezardUi.Native]::SetProcessDpiAwarenessContext([IntPtr]-4)
$A = [Windows.Automation.AutomationElement]
$T = [Windows.Automation.ControlType]
$kinds = @($T::Button, $T::SplitButton, $T::MenuItem, $T::Hyperlink, $T::Edit, $T::ComboBox, $T::ListItem,
           $T::TabItem, $T::TreeItem, $T::CheckBox, $T::RadioButton, $T::DataItem)
$cond = New-Object Windows.Automation.OrCondition (
    [Windows.Automation.Condition[]]($kinds | ForEach-Object { New-Object Windows.Automation.PropertyCondition ($A::ControlTypeProperty, $_) }))
$roots = @()
$fg = [LezardUi.Native]::GetForegroundWindow()
if ($fg -ne [IntPtr]::Zero) { $roots += $A::FromHandle($fg) }
$tray = $A::RootElement.FindFirst([Windows.Automation.TreeScope]::Children,
    (New-Object Windows.Automation.PropertyCondition ($A::ClassNameProperty, 'Shell_TrayWnd')))
if ($tray) { $roots += $tray }
$n = 0
foreach ($root in $roots) {
    foreach ($e in $root.FindAll([Windows.Automation.TreeScope]::Descendants, $cond)) {
        $c = $e.Current
        $b = $c.BoundingRectangle
        if ($c.IsOffscreen -or $b.Width -lt 6 -or $b.Height -lt 6 -or [double]::IsInfinity($b.X)) { continue }
        $name = ($c.Name -replace '\s+', ' ').Trim()
        if (-not $name -and $c.ControlType -ne $T::Edit) { continue }
        "{0}`t{1}`t{2}`t{3}`t{4}`t{5}" -f $c.ControlType.ProgrammaticName.Substring(12), $name, [int]$b.X, [int]$b.Y,
            [int]$b.Width, [int]$b.Height
        if (++$n -ge 150) { exit }
    }
}
