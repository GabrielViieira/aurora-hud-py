$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voices = @(
        $synth.GetInstalledVoices() |
            Where-Object { $_.Enabled } |
            ForEach-Object {
                [PSCustomObject]@{
                    Name    = $_.VoiceInfo.Name
                    Culture = $_.VoiceInfo.Culture.Name
                    Gender  = $_.VoiceInfo.Gender.ToString()
                }
            }
    )
    ConvertTo-Json -InputObject $voices -Compress
} finally {
    $synth.Dispose()
}
