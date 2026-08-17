package connector

import "testing"

func TestRegistryContainsCanonicalConnectors(t *testing.T) {
	r := NewRegistry(Options{BrowserURL: "http://browser.invalid"})
	keys := r.Keys()
	if len(keys) != 24 {
		t.Fatalf("registry has %d keys, want 24", len(keys))
	}
	if got := len(r.All()); got != len(keys) {
		t.Fatalf("Registry.All returned %d connectors, want %d", got, len(keys))
	}
	for _, key := range keys {
		c, ok := r.Get(key)
		if !ok || c == nil {
			t.Fatalf("Registry.Get(%q) did not return a connector", key)
		}
		if c.Key() != key {
			t.Errorf("Registry.Get(%q).Key() = %q", key, c.Key())
		}
	}
}

func TestRegistryRejectsUnknownConnector(t *testing.T) {
	r := NewRegistry(Options{})
	if c, ok := r.Get("unknown"); ok || c != nil {
		t.Fatalf("Registry.Get(unknown) = (%v, %v), want (nil, false)", c, ok)
	}
}

func TestRegistryWiresIACRBrowserTransport(t *testing.T) {
	c, ok := NewRegistry(Options{BrowserURL: "http://browser.test"}).Get("iacr")
	if !ok {
		t.Fatal("IACR connector missing")
	}
	iacr, ok := c.(*iacrC)
	if !ok || iacr.browser == nil {
		t.Fatalf("IACR browser transport not wired: %#v", c)
	}
	if iacr.browser.BaseURL != "http://browser.test" {
		t.Errorf("IACR browser URL = %q", iacr.browser.BaseURL)
	}
}
