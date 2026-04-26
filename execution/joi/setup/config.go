package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// --- Env file ---

type EnvLine struct {
	Raw       string // preserved verbatim for non-key lines
	Key       string // empty for plain comments/blanks
	Value     string
	Commented bool // true = #KEY=VALUE
}

var envKeyRe = regexp.MustCompile(`^#?([A-Z][A-Z0-9_]*)=(.*)$`)

func loadEnvFile(path string) ([]EnvLine, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read env file: %w", err)
	}
	var lines []EnvLine
	for _, raw := range strings.Split(string(data), "\n") {
		m := envKeyRe.FindStringSubmatch(raw)
		if m != nil {
			commented := strings.HasPrefix(raw, "#")
			lines = append(lines, EnvLine{
				Raw:       raw,
				Key:       m[1],
				Value:     m[2],
				Commented: commented,
			})
		} else {
			lines = append(lines, EnvLine{Raw: raw})
		}
	}
	return lines, nil
}

func writeEnvFile(path string, lines []EnvLine) error {
	var buf strings.Builder
	for i, l := range lines {
		if l.Key != "" {
			if l.Commented {
				buf.WriteString("#" + l.Key + "=" + l.Value)
			} else {
				buf.WriteString(l.Key + "=" + l.Value)
			}
		} else {
			buf.WriteString(l.Raw)
		}
		if i < len(lines)-1 {
			buf.WriteByte('\n')
		}
	}
	// Ensure trailing newline
	s := buf.String()
	if !strings.HasSuffix(s, "\n") {
		s += "\n"
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(s), 0644); err != nil {
		return fmt.Errorf("write env tmp: %w", err)
	}
	return os.Rename(tmp, path)
}

// --- Policy JSON ---

func defaultPolicyDoc() map[string]any {
	return map[string]any{
		"version": float64(1),
		"mode":    "companion",
		"identity": map[string]any{
			"bot_name":        "Joi",
			"owner_id":        nil,
			"allowed_senders": []any{},
			"groups":          map[string]any{},
		},
		"rate_limits": map[string]any{
			"inbound": map[string]any{
				"max_per_hour":   float64(120),
				"max_per_minute": float64(20),
			},
		},
		"validation": map[string]any{
			"max_text_length":      float64(1500),
			"max_timestamp_skew_ms": float64(300000),
		},
		"security": map[string]any{
			"privacy_mode": true,
			"kill_switch":  false,
		},
		"routing": map[string]any{
			"enabled":         false,
			"default_backend": "joi",
			"backends": map[string]any{
				"joi": map[string]any{"url": "http://10.42.0.10:8443"},
			},
			"rules": []any{},
		},
		"wind": map[string]any{
			"enabled":               false,
			"shadow_mode":           true,
			"quiet_hours_start":     float64(23),
			"quiet_hours_end":       float64(7),
			"min_cooldown_seconds":  float64(3600),
			"daily_cap":             float64(3),
			"max_unanswered_streak": float64(2),
			"min_silence_seconds":   float64(1800),
			"impulse_threshold":     0.6,
			"base_impulse":          0.1,
			"silence_weight":        0.3,
			"silence_cap_hours":     24.0,
			"topic_pressure_weight": 0.2,
			"fatigue_weight":        0.3,
			"allowlist":             []any{},
		},
	}
}

func loadPolicyJSON(path string) (map[string]any, error) {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return defaultPolicyDoc(), nil
	}
	if err != nil {
		return nil, fmt.Errorf("read policy: %w", err)
	}
	var doc map[string]any
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("parse policy JSON: %w", err)
	}
	// Fill missing sections from defaults
	defaults := defaultPolicyDoc()
	if _, ok := doc["wind"]; !ok {
		doc["wind"] = defaults["wind"]
	}
	if _, ok := doc["security"]; !ok {
		doc["security"] = defaults["security"]
	}
	return doc, nil
}

func writePolicyJSON(path string, doc map[string]any) error {
	data, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal policy: %w", err)
	}
	data = append(data, '\n')
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0644); err != nil {
		return fmt.Errorf("write policy tmp: %w", err)
	}
	return os.Rename(tmp, path)
}

// jsonToString converts a JSON value to a string based on the setting type.
func jsonToString(val any, typ ValType) string {
	if val == nil {
		return ""
	}
	switch typ {
	case TypeBool:
		return fmt.Sprintf("%v", val)
	case TypeInt:
		if f, ok := val.(float64); ok {
			return strconv.Itoa(int(f))
		}
		return fmt.Sprintf("%v", val)
	case TypeFloat:
		if f, ok := val.(float64); ok {
			return strconv.FormatFloat(f, 'f', -1, 64)
		}
		return fmt.Sprintf("%v", val)
	default:
		return fmt.Sprintf("%v", val)
	}
}

// stringToJSON converts a string setting value back to a typed JSON value.
func stringToJSON(val string, typ ValType) any {
	switch typ {
	case TypeBool:
		return val == "true"
	case TypeInt:
		n, _ := strconv.Atoi(val)
		return float64(n)
	case TypeFloat:
		f, _ := strconv.ParseFloat(val, 64)
		return f
	default:
		return val
	}
}

// --- Prompts directory ---

var promptExts = []struct {
	ext   string
	label string
	typ   ValType
	field string // Model, Context, Compact, Prompt
}{
	{".model", "Model", TypeString, "Model"},
	{".context", "Context msgs", TypeInt, "Context"},
	{".compact_window", "Compact batch", TypeInt, "Compact"},
	{".txt", "System prompt", TypeText, "Prompt"},
}

func discoverConversations(promptsDir string) ([]*ConvOverride, error) {
	convMap := make(map[string]*ConvOverride) // key = "scope/id"

	for _, scope := range []string{"users", "groups"} {
		dir := filepath.Join(promptsDir, scope)
		entries, err := os.ReadDir(dir)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("read %s: %w", dir, err)
		}
		for _, e := range entries {
			if e.IsDir() {
				continue
			}
			name := e.Name()
			for _, pe := range promptExts {
				if !strings.HasSuffix(name, pe.ext) {
					continue
				}
				id := strings.TrimSuffix(name, pe.ext)
				if id == "" {
					continue
				}
				key := scope + "/" + id
				conv, ok := convMap[key]
				if !ok {
					conv = &ConvOverride{ID: id, Scope: scope}
					convMap[key] = conv
				}
				// Read file content
				content, err := os.ReadFile(filepath.Join(dir, name))
				if err != nil {
					continue
				}
				val := strings.TrimSpace(string(content))
				s := &Setting{
					Label:        pe.label,
					Key:          pe.ext,
					Type:         pe.typ,
					Value:        val,
					OnDisk:       val,
					Active:       true,
					OnDiskActive: true,
				}
				switch pe.field {
				case "Model":
					conv.Model = s
				case "Context":
					conv.Context = s
				case "Compact":
					conv.Compact = s
				case "Prompt":
					// For text, preserve original content (not trimmed)
					s.Value = string(content)
					s.OnDisk = string(content)
					conv.Prompt = s
				}
			}
		}
	}

	result := make([]*ConvOverride, 0, len(convMap))
	for _, c := range convMap {
		result = append(result, c)
	}
	return result, nil
}

// --- Config orchestration ---

const (
	defaultEnvPath    = "/etc/default/joi-api"
	defaultPolicyPath = "/var/lib/joi/policy/mesh-policy.json"
	defaultPromptsDir = "/var/lib/joi/prompts"
)

type Config struct {
	EnvPath    string
	PolicyPath string
	PromptsDir string

	EnvLines  []EnvLine
	PolicyDoc map[string]any

	HW       *Area
	Wind     *Area
	Security *Area
	Convos   []*ConvOverride
}

func LoadAll() (*Config, error) {
	cfg := &Config{EnvPath: defaultEnvPath}

	// Load env file
	lines, err := loadEnvFile(cfg.EnvPath)
	if err != nil {
		return nil, err
	}
	cfg.EnvLines = lines

	// Extract paths from parsed env lines (not os.Getenv)
	cfg.PolicyPath = envValue(lines, "JOI_MESH_POLICY_PATH", defaultPolicyPath)
	cfg.PromptsDir = envValue(lines, "JOI_PROMPTS_DIR", defaultPromptsDir)

	// Load policy JSON
	doc, err := loadPolicyJSON(cfg.PolicyPath)
	if err != nil {
		return nil, err
	}
	cfg.PolicyDoc = doc

	// Populate HW settings from env lines
	cfg.HW = &Area{Name: "HW / Runtime"}
	for _, def := range hwDefs {
		s := &Setting{
			Label:   def.Label,
			Key:     def.Key,
			Type:    def.Type,
			Default: def.Default,
		}
		found := false
		for _, l := range cfg.EnvLines {
			if l.Key == def.Key {
				found = true
				if l.Commented {
					s.Value = def.Default
					s.Active = false
				} else {
					s.Value = l.Value
					s.Active = true
				}
				break
			}
		}
		if !found {
			// Append a commented env line so write can find it
			cfg.EnvLines = append(cfg.EnvLines, EnvLine{
				Key:       def.Key,
				Value:     def.Default,
				Commented: true,
			})
			s.Value = def.Default
			s.Active = false
		}
		s.OnDisk = s.Value
		s.OnDiskActive = s.Active
		cfg.HW.Settings = append(cfg.HW.Settings, s)
	}

	// Populate Wind settings from policy JSON
	cfg.Wind = &Area{Name: "Wind"}
	windSection, _ := doc["wind"].(map[string]any)
	for _, def := range windDefs {
		s := &Setting{
			Label:   def.Label,
			Key:     def.Key,
			Type:    def.Type,
			Default: def.Default,
			Active:  true,
		}
		if val, ok := windSection[def.Key]; ok {
			s.Value = jsonToString(val, def.Type)
		} else {
			s.Value = def.Default
		}
		s.OnDisk = s.Value
		s.OnDiskActive = true
		cfg.Wind.Settings = append(cfg.Wind.Settings, s)
	}

	// Populate Security settings from policy JSON
	cfg.Security = &Area{Name: "Security"}
	secSection, _ := doc["security"].(map[string]any)
	for _, def := range securityDefs {
		s := &Setting{
			Label:   def.Label,
			Key:     def.Key,
			Type:    def.Type,
			Default: def.Default,
			Active:  true,
		}
		if val, ok := secSection[def.Key]; ok {
			s.Value = jsonToString(val, def.Type)
		} else {
			s.Value = def.Default
		}
		s.OnDisk = s.Value
		s.OnDiskActive = true
		cfg.Security.Settings = append(cfg.Security.Settings, s)
	}

	// Discover conversations
	convos, err := discoverConversations(cfg.PromptsDir)
	if err != nil {
		return nil, err
	}
	cfg.Convos = convos

	return cfg, nil
}

// envValue scans parsed env lines for a key, returning its value if active,
// or the fallback if not found or commented out.
func envValue(lines []EnvLine, key, fallback string) string {
	for _, l := range lines {
		if l.Key == key && !l.Commented {
			return l.Value
		}
	}
	return fallback
}

func backupFile(path string) error {
	stamp := time.Now().Format("20060102-1504")
	dst := path + ".bak." + stamp
	src, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("backup open %s: %w", path, err)
	}
	defer src.Close()
	out, err := os.Create(dst)
	if err != nil {
		return fmt.Errorf("backup create %s: %w", dst, err)
	}
	defer out.Close()
	if _, err := io.Copy(out, src); err != nil {
		return fmt.Errorf("backup copy: %w", err)
	}
	return nil
}

func (cfg *Config) TotalDirty() int {
	n := cfg.HW.DirtyCount() + cfg.Wind.DirtyCount() + cfg.Security.DirtyCount()
	for _, c := range cfg.Convos {
		n += c.DirtyCount()
	}
	return n
}

// DirtyChanges returns human-readable descriptions of all pending changes.
func (cfg *Config) DirtyChanges() []string {
	var changes []string
	for _, area := range []*Area{cfg.HW, cfg.Wind, cfg.Security} {
		for _, s := range area.Settings {
			if s.IsDirty() {
				changes = append(changes, fmt.Sprintf("%s / %s: %s -> %s",
					area.Name, s.Label, displayValue(s, true), displayValue(s, false)))
			}
		}
	}
	for _, c := range cfg.Convos {
		for _, s := range c.AllSettings() {
			if s != nil && s.IsDirty() {
				action := fmt.Sprintf("%s (%s) / %s: ", c.ID, c.Scope, s.Label)
				if s.Deleted {
					action += "remove"
				} else if s.OnDisk == "" && !s.OnDiskActive {
					action += "(new) " + s.Value
				} else {
					action += s.OnDisk + " -> " + s.Value
				}
				changes = append(changes, action)
			}
		}
	}
	return changes
}

// displayValue returns old (disk) or new (memory) value for display.
func displayValue(s *Setting, old bool) string {
	if old {
		if !s.OnDiskActive {
			return "(default)"
		}
		return s.OnDisk
	}
	if !s.Active {
		return "(default)"
	}
	return s.Value
}

func (cfg *Config) Apply() error {
	// Collect files to back up
	backups := make(map[string]bool)
	if cfg.HW.IsDirty() {
		backups[cfg.EnvPath] = true
	}
	if cfg.Wind.IsDirty() || cfg.Security.IsDirty() {
		backups[cfg.PolicyPath] = true
	}
	for _, c := range cfg.Convos {
		for i, s := range c.AllSettings() {
			if s == nil || !s.IsDirty() {
				continue
			}
			ext := promptExts[i].ext
			path := filepath.Join(cfg.PromptsDir, c.Scope, c.ID+ext)
			if _, err := os.Stat(path); err == nil {
				backups[path] = true
			}
		}
	}

	// Backup existing files
	for path := range backups {
		if err := backupFile(path); err != nil {
			return fmt.Errorf("backup failed: %w", err)
		}
	}

	// Stop service
	out, err := exec.Command("systemctl", "stop", "joi-api").CombinedOutput()
	if err != nil {
		msg := strings.ToLower(string(out))
		if !strings.Contains(msg, "not loaded") && !strings.Contains(msg, "not found") {
			return fmt.Errorf("systemctl stop joi-api: %s (%w)", strings.TrimSpace(string(out)), err)
		}
	}

	// Write env file
	if cfg.HW.IsDirty() {
		cfg.syncHWToEnvLines()
		if err := writeEnvFile(cfg.EnvPath, cfg.EnvLines); err != nil {
			return fmt.Errorf("write env: %w", err)
		}
	}

	// Write policy JSON
	if cfg.Wind.IsDirty() || cfg.Security.IsDirty() {
		cfg.syncPolicyToDoc()
		if err := writePolicyJSON(cfg.PolicyPath, cfg.PolicyDoc); err != nil {
			return fmt.Errorf("write policy: %w", err)
		}
	}

	// Write/delete prompt files
	for _, c := range cfg.Convos {
		for i, s := range c.AllSettings() {
			if s == nil || !s.IsDirty() {
				continue
			}
			ext := promptExts[i].ext
			path := filepath.Join(cfg.PromptsDir, c.Scope, c.ID+ext)
			if s.Deleted {
				os.Remove(path)
			} else {
				if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
					return fmt.Errorf("mkdir %s: %w", filepath.Dir(path), err)
				}
				if err := os.WriteFile(path, []byte(s.Value), 0644); err != nil {
					return fmt.Errorf("write %s: %w", path, err)
				}
			}
		}
	}

	return nil
}

func (cfg *Config) syncHWToEnvLines() {
	for _, s := range cfg.HW.Settings {
		if !s.IsDirty() {
			continue
		}
		for i, l := range cfg.EnvLines {
			if l.Key == s.Key {
				cfg.EnvLines[i].Commented = !s.Active
				cfg.EnvLines[i].Value = s.Value
				break
			}
		}
	}
}

func (cfg *Config) syncPolicyToDoc() {
	windSection, ok := cfg.PolicyDoc["wind"].(map[string]any)
	if !ok {
		windSection = make(map[string]any)
		cfg.PolicyDoc["wind"] = windSection
	}
	for _, s := range cfg.Wind.Settings {
		if s.IsDirty() {
			windSection[s.Key] = stringToJSON(s.Value, s.Type)
		}
	}

	secSection, ok := cfg.PolicyDoc["security"].(map[string]any)
	if !ok {
		secSection = make(map[string]any)
		cfg.PolicyDoc["security"] = secSection
	}
	for _, s := range cfg.Security.Settings {
		if s.IsDirty() {
			secSection[s.Key] = stringToJSON(s.Value, s.Type)
		}
	}
}
