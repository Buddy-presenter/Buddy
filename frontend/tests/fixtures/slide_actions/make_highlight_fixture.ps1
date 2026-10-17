# Original Buddy content; PowerPoint is used only to regenerate this checked-in export.
$ErrorActionPreference = 'Stop'
$powerPoint = New-Object -ComObject PowerPoint.Application
try {
    $presentation = $powerPoint.Presentations.Add(0)
    try {
        $presentation.PageSetup.SlideWidth = 960
        $presentation.PageSetup.SlideHeight = 540
        $slide = $presentation.Slides.Add(1, 12)
        $slide.FollowMasterBackground = 0
        $slide.Background.Fill.ForeColor.RGB = 0x201810
        foreach ($row in @(@('Highlight any content',32), @('Buddy',140),
                           @('Focus on one idea.',220), @('price × quantity',300), @('E = mc²',380))) {
            $shape = $slide.Shapes.AddTextbox(1, 40, $row[1], 880, 70)
            $shape.TextFrame.TextRange.Text = $row[0]
            $shape.TextFrame.TextRange.Font.Name = 'Arial'
            $shape.TextFrame.TextRange.Font.Size = 28
            $shape.TextFrame.TextRange.Font.Color.RGB = 0xFFFFFF
        }
        $presentation.SaveAs((Join-Path $PSScriptRoot 'content.pptx'),24)
        $slide.Export((Join-Path $PSScriptRoot 'content.png'),'PNG',960,540)
    } finally {
        $presentation.Close()
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($presentation)
    }
} finally {
    $powerPoint.Quit()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($powerPoint)
}
