package service

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"image"
	_ "image/gif"
	"image/png"
	"math"
)

type perelmanImage struct {
	ID     string
	Data   []byte
	MIME   string
	Kind   string
	Width  int
	Height int
}

type imageRegistry struct {
	images      map[string]perelmanImage
	maxImageDim int
}

func newImageRegistry(maxDims ...int) *imageRegistry {
	maxDim := 4096
	if len(maxDims) > 0 && maxDims[0] > 0 {
		maxDim = maxDims[0]
	}
	return &imageRegistry{images: make(map[string]perelmanImage), maxImageDim: maxDim}
}

func (r *imageRegistry) add(img perelmanImage) {
	r.images[img.ID] = img
}

func (r *imageRegistry) get(id string) (perelmanImage, error) {
	img, ok := r.images[id]
	if !ok {
		return perelmanImage{}, fmt.Errorf("unknown image_id %q", id)
	}
	return img, nil
}

func (r *imageRegistry) dataURI(id string) (string, error) {
	img, err := r.get(id)
	if err != nil {
		return "", err
	}
	return "data:" + img.MIME + ";base64," + base64.StdEncoding.EncodeToString(img.Data), nil
}

func encodePNG(img image.Image) ([]byte, error) {
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		return nil, fmt.Errorf("encode image: %w", err)
	}
	return buf.Bytes(), nil
}

func decodeImage(data []byte) (image.Image, image.Point, error) {
	img, format, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		return nil, image.Point{}, fmt.Errorf("decode image: %w", err)
	}
	if format == "" {
		return nil, image.Point{}, fmt.Errorf("image format is empty")
	}
	return img, img.Bounds().Size(), nil
}

func resizeImage(src image.Image, width, height int) image.Image {
	dst := image.NewRGBA(image.Rect(0, 0, width, height))
	sb := src.Bounds()
	for y := 0; y < height; y++ {
		sy := sb.Min.Y + y*sb.Dy()/height
		for x := 0; x < width; x++ {
			sx := sb.Min.X + x*sb.Dx()/width
			dst.Set(x, y, src.At(sx, sy))
		}
	}
	return dst
}

func toolImage(reg *imageRegistry, id string, img image.Image, kind string) (string, error) {
	bounds := img.Bounds()
	if longest := maxInt(bounds.Dx(), bounds.Dy()); longest > reg.maxImageDim {
		scale := float64(reg.maxImageDim) / float64(longest)
		img = resizeImage(img, maxInt(1, int(float64(bounds.Dx())*scale)), maxInt(1, int(float64(bounds.Dy())*scale)))
	}
	data, err := encodePNG(img)
	if err != nil {
		return "", err
	}
	name := fmt.Sprintf("%s-tool-%d", id, len(reg.images))
	bounds = img.Bounds()
	reg.add(perelmanImage{ID: name, Data: data, MIME: "image/png", Kind: kind, Width: bounds.Dx(), Height: bounds.Dy()})
	return name, nil
}

func dispatchImageTool(reg *imageRegistry, name string, arguments map[string]any) (string, error) {
	id, ok := arguments["image_id"].(string)
	if !ok || id == "" {
		return "", fmt.Errorf("%s requires image_id", name)
	}
	original, err := reg.get(id)
	if err != nil {
		return "", err
	}
	src, size, err := decodeImage(original.Data)
	if err != nil {
		return "", err
	}
	switch name {
	case "zoom":
		factor, ok := number(arguments["factor"])
		if !ok || factor <= 0 || factor > 8 {
			return "", fmt.Errorf("zoom factor must be in (0, 8]")
		}
		return toolImage(reg, id, resizeImage(src, maxInt(1, int(math.Round(float64(size.X)*factor))), maxInt(1, int(math.Round(float64(size.Y)*factor)))), "tool-result")
	case "crop":
		region, ok := arguments["region"].(map[string]any)
		if !ok {
			return "", fmt.Errorf("crop requires region")
		}
		x, xok := number(region["x"])
		y, yok := number(region["y"])
		w, wok := number(region["w"])
		h, hok := number(region["h"])
		if !xok || !yok || !wok || !hok || x < 0 || y < 0 || w <= 0 || h <= 0 || x+w > float64(size.X) || y+h > float64(size.Y) {
			return "", fmt.Errorf("crop region is outside image bounds %dx%d", size.X, size.Y)
		}
		bounds := image.Rect(int(x), int(y), int(x+w), int(y+h))
		cropped := image.NewRGBA(image.Rect(0, 0, bounds.Dx(), bounds.Dy()))
		for cy := 0; cy < bounds.Dy(); cy++ {
			for cx := 0; cx < bounds.Dx(); cx++ {
				cropped.Set(cx, cy, src.At(bounds.Min.X+cx, bounds.Min.Y+cy))
			}
		}
		return toolImage(reg, id, cropped, "tool-result")
	case "rotate":
		degrees, ok := number(arguments["degrees"])
		if !ok || int(degrees)%90 != 0 {
			return "", fmt.Errorf("rotate degrees must be a multiple of 90")
		}
		return toolImage(reg, id, rotateImage(src, int(degrees)), "tool-result")
	default:
		return "", fmt.Errorf("unknown image tool %q", name)
	}
}

func number(value any) (float64, bool) {
	switch typed := value.(type) {
	case float64:
		return typed, true
	case json.Number:
		v, err := typed.Float64()
		return v, err == nil
	default:
		return 0, false
	}
}

func rotateImage(src image.Image, degrees int) image.Image {
	degrees = ((degrees % 360) + 360) % 360
	b := src.Bounds()
	if degrees == 0 {
		return src
	}
	width, height := b.Dx(), b.Dy()
	if degrees == 90 || degrees == 270 {
		width, height = height, width
	}
	dst := image.NewRGBA(image.Rect(0, 0, width, height))
	for y := 0; y < b.Dy(); y++ {
		for x := 0; x < b.Dx(); x++ {
			dx, dy := x, y
			switch degrees {
			case 90:
				dx, dy = b.Dy()-1-y, x
			case 180:
				dx, dy = b.Dx()-1-x, b.Dy()-1-y
			case 270:
				dx, dy = y, b.Dx()-1-x
			}
			dst.Set(dx, dy, src.At(b.Min.X+x, b.Min.Y+y))
		}
	}
	return dst
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

var perelmanToolSchemas = []map[string]any{
	{"type": "function", "function": map[string]any{"name": "zoom", "description": "Zoom an image for closer inspection of formulas, axes, or table cells.", "parameters": map[string]any{"type": "object", "properties": map[string]any{"image_id": map[string]any{"type": "string"}, "factor": map[string]any{"type": "number"}}, "required": []string{"image_id", "factor"}, "additionalProperties": false}}},
	{"type": "function", "function": map[string]any{"name": "crop", "description": "Crop an image using source-pixel coordinates.", "parameters": map[string]any{"type": "object", "properties": map[string]any{"image_id": map[string]any{"type": "string"}, "region": map[string]any{"type": "object", "properties": map[string]any{"x": map[string]any{"type": "number"}, "y": map[string]any{"type": "number"}, "w": map[string]any{"type": "number"}, "h": map[string]any{"type": "number"}}, "required": []string{"x", "y", "w", "h"}, "additionalProperties": false}}, "required": []string{"image_id", "region"}, "additionalProperties": false}}},
	{"type": "function", "function": map[string]any{"name": "rotate", "description": "Rotate an image by a multiple of 90 degrees.", "parameters": map[string]any{"type": "object", "properties": map[string]any{"image_id": map[string]any{"type": "string"}, "degrees": map[string]any{"type": "integer"}}, "required": []string{"image_id", "degrees"}, "additionalProperties": false}}},
}
