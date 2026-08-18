package service

import (
	"bytes"
	"image"
	"image/color"
	"image/png"
	"testing"
)

func testPNG(t *testing.T, width, height int) []byte {
	t.Helper()
	img := image.NewRGBA(image.Rect(0, 0, width, height))
	for y := 0; y < height; y++ {
		for x := 0; x < width; x++ {
			img.Set(x, y, color.RGBA{R: uint8(x * 40), G: uint8(y * 40), A: 255})
		}
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestDispatchImageToolCropZoomRotate(t *testing.T) {
	reg := newImageRegistry()
	reg.add(perelmanImage{ID: "page-0", Data: testPNG(t, 4, 2), MIME: "image/png", Width: 4, Height: 2})

	cropID, err := dispatchImageTool(reg, "crop", map[string]any{
		"image_id": "page-0", "region": map[string]any{"x": 1.0, "y": 0.0, "w": 2.0, "h": 2.0},
	})
	if err != nil {
		t.Fatal(err)
	}
	crop, err := reg.get(cropID)
	if err != nil || crop.Width != 2 || crop.Height != 2 {
		t.Fatalf("crop = %#v, err=%v", crop, err)
	}

	zoomID, err := dispatchImageTool(reg, "zoom", map[string]any{"image_id": cropID, "factor": 2.0})
	if err != nil {
		t.Fatal(err)
	}
	zoom, _ := reg.get(zoomID)
	if zoom.Width != 4 || zoom.Height != 4 {
		t.Fatalf("zoom dimensions = %dx%d", zoom.Width, zoom.Height)
	}

	rotateID, err := dispatchImageTool(reg, "rotate", map[string]any{"image_id": "page-0", "degrees": 90.0})
	if err != nil {
		t.Fatal(err)
	}
	rotated, _ := reg.get(rotateID)
	if rotated.Width != 2 || rotated.Height != 4 {
		t.Fatalf("rotate dimensions = %dx%d", rotated.Width, rotated.Height)
	}
}

func TestDispatchImageToolRejectsOutOfBoundsCrop(t *testing.T) {
	reg := newImageRegistry()
	reg.add(perelmanImage{ID: "page-0", Data: testPNG(t, 4, 2), MIME: "image/png", Width: 4, Height: 2})
	if _, err := dispatchImageTool(reg, "crop", map[string]any{
		"image_id": "page-0", "region": map[string]any{"x": 3.0, "y": 0.0, "w": 2.0, "h": 1.0},
	}); err == nil {
		t.Fatal("expected crop bounds error")
	}
}
