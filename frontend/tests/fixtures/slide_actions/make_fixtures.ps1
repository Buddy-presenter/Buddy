# Buddy-authored content. PowerPoint is needed only to regenerate these real exports.
# Run from any directory on Windows with PowerPoint installed; CI uses checked-in PNGs.
$ErrorActionPreference = 'Stop'
$fixtureRoot = $PSScriptRoot
$powerPoint = New-Object -ComObject PowerPoint.Application
try {
    foreach ($spec in @(@('wide', 960, 540), @('standard', 720, 540), @('portrait', 540, 720))) {
        $presentation = $powerPoint.Presentations.Add(0)
        try {
            $presentation.PageSetup.SlideWidth = $spec[1]
            $presentation.PageSetup.SlideHeight = $spec[2]
            $slide = $presentation.Slides.Add(1, 12)
            $slide.FollowMasterBackground = 0
            $slide.Background.Fill.ForeColor.RGB = 0x201810
            $title = $slide.Shapes.AddTextbox(1, 40, 32, ($spec[1] - 80), 100)
            $title.TextFrame.TextRange.Text = 'Clear slides. Confident delivery.'
            $title.TextFrame.TextRange.Font.Name = 'Arial'
            $title.TextFrame.TextRange.Font.Size = 32
            $title.TextFrame.TextRange.Font.Color.RGB = 0xFFFFFF
            $body = $slide.Shapes.AddTextbox(1, 40, 160, ($spec[1] - 80), 180)
            $body.TextFrame.TextRange.Text = "Prepare your story.`r`nFocus on one idea.`r`nReturn to the full picture."
            $body.TextFrame.TextRange.Font.Name = 'Arial'
            $body.TextFrame.TextRange.Font.Size = 24
            $body.TextFrame.TextRange.Font.Color.RGB = 0xD9E4F0
            $band = $slide.Shapes.AddShape(1, 40, ($spec[2] - 100), ($spec[1] - 80), 48)
            $band.Fill.ForeColor.RGB = 0x3D84F5
            $band.Line.Visible = 0
            $band.TextFrame.TextRange.Text = "Buddy / $($spec[0]) / original test presentation"
            $band.TextFrame.TextRange.Font.Size = 18
            $band.TextFrame.TextRange.Font.Color.RGB = 0x101820
            $presentation.SaveAs((Join-Path $fixtureRoot "$($spec[0]).pptx"), 24)
            $slide.Export((Join-Path $fixtureRoot "$($spec[0]).png"), 'PNG', $spec[1], $spec[2])
        } finally {
            $presentation.Close()
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($presentation)
        }
    }
} finally {
    $powerPoint.Quit()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($powerPoint)
}
