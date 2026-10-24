param(
    [string]$TextFile,
    [string]$VoiceName = "",
    [int]$Rate = 0,
    [int]$Volume = 100
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($VoiceName) {
        $synth.SelectVoice($VoiceName)
    } else {
        $voice = $synth.GetInstalledVoices() |
            Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like "ja*" } |
            Select-Object -First 1
        if ($voice) { $synth.SelectVoice($voice.VoiceInfo.Name) }
    }
    $synth.Rate = [Math]::Max(-10, [Math]::Min(10, $Rate))
    $synth.Volume = [Math]::Max(0, [Math]::Min(100, $Volume))
    $synth.Speak($text)
} finally {
    $synth.Dispose()
}
