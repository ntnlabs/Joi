package main

import "strings"

type ValType int

const (
	TypeBool ValType = iota
	TypeInt
	TypeFloat
	TypeString
	TypeText // multi-line, edited via $EDITOR
)

type Setting struct {
	Label        string
	Key          string
	Type         ValType
	Default      string
	Value        string
	OnDisk       string
	Active       bool
	OnDiskActive bool
	Deleted      bool // conversation only: marked for file deletion on Apply
}

func (s *Setting) IsDirty() bool {
	return s.Value != s.OnDisk || s.Active != s.OnDiskActive || s.Deleted
}

type Area struct {
	Name     string
	Settings []*Setting
}

func (a *Area) IsDirty() bool {
	for _, s := range a.Settings {
		if s.IsDirty() {
			return true
		}
	}
	return false
}

func (a *Area) DirtyCount() int {
	n := 0
	for _, s := range a.Settings {
		if s.IsDirty() {
			n++
		}
	}
	return n
}

type ConvOverride struct {
	ID      string   // sanitized filename base
	Scope   string   // "users" or "groups"
	Model   *Setting // nil = no override file on disk
	Context *Setting
	Compact   *Setting
	Prompt    *Setting
	Translate *Setting
	IsNew     bool // true if created in this session
}

func (c *ConvOverride) IsDirty() bool {
	for _, s := range c.AllSettings() {
		if s != nil && s.IsDirty() {
			return true
		}
	}
	return false
}

func (c *ConvOverride) DirtyCount() int {
	n := 0
	for _, s := range c.AllSettings() {
		if s != nil && s.IsDirty() {
			n++
		}
	}
	return n
}

func (c *ConvOverride) AllSettings() []*Setting {
	return []*Setting{c.Model, c.Context, c.Compact, c.Prompt, c.Translate}
}

// Tags returns short labels for which overrides exist.
func (c *ConvOverride) Tags() string {
	var tags []string
	if c.Model != nil && !c.Model.Deleted {
		tags = append(tags, "model")
	}
	if c.Context != nil && !c.Context.Deleted {
		tags = append(tags, "ctx")
	}
	if c.Compact != nil && !c.Compact.Deleted {
		tags = append(tags, "compact")
	}
	if c.Prompt != nil && !c.Prompt.Deleted {
		tags = append(tags, "prompt")
	}
	if c.Translate != nil && !c.Translate.Deleted {
		tags = append(tags, "translate")
	}
	if len(tags) == 0 {
		return "no overrides"
	}
	return strings.Join(tags, " ")
}

func sanitizeScope(scope string) string {
	scope = strings.TrimSpace(scope)
	if scope == "" {
		return ""
	}
	scope = strings.ReplaceAll(scope, "/", "_")
	scope = strings.ReplaceAll(scope, "\\", "_")
	scope = strings.ReplaceAll(scope, "+", "-")
	for strings.Contains(scope, "..") {
		scope = strings.ReplaceAll(scope, "..", "_")
	}
	return scope
}

// Setting definition tables

type settingDef struct {
	Label   string
	Key     string
	Type    ValType
	Default string
}

var hwDefs = []settingDef{
	{"Model", "JOI_OLLAMA_MODEL", TypeString, "llama3"},
	{"Timeout", "JOI_LLM_TIMEOUT", TypeInt, "180"},
	{"Keep alive", "JOI_LLM_KEEP_ALIVE", TypeString, "30m"},
	{"Num ctx", "JOI_OLLAMA_NUM_CTX", TypeInt, "0"},
	{"Context msgs", "JOI_CONTEXT_MESSAGES", TypeInt, "50"},
	{"Compact batch", "JOI_COMPACT_BATCH_SIZE", TypeInt, "20"},
	{"RAG max tokens", "JOI_RAG_MAX_TOKENS", TypeInt, "1500"},
	{"RAG min similarity", "JOI_RAG_MIN_SIMILARITY", TypeFloat, "0.45"},
	{"Max input length", "JOI_MAX_INPUT_LENGTH", TypeInt, "1500"},
	{"Max output length", "JOI_MAX_OUTPUT_LENGTH", TypeInt, "2000"},
	{"Translate prefix", "JOI_TRANSLATE_MODEL_PREFIX", TypeString, "translategemma"},
}

var windDefs = []settingDef{
	{"Enabled", "enabled", TypeBool, "false"},
	{"Shadow mode", "shadow_mode", TypeBool, "true"},
	{"Daily cap", "daily_cap", TypeInt, "3"},
	{"Quiet hours start", "quiet_hours_start", TypeInt, "23"},
	{"Quiet hours end", "quiet_hours_end", TypeInt, "7"},
	{"Min cooldown (sec)", "min_cooldown_seconds", TypeInt, "3600"},
	{"Min silence (sec)", "min_silence_seconds", TypeInt, "1800"},
	{"Max unanswered", "max_unanswered_streak", TypeInt, "2"},
	{"Impulse threshold", "impulse_threshold", TypeFloat, "0.6"},
	{"Base impulse", "base_impulse", TypeFloat, "0.1"},
	{"Silence weight", "silence_weight", TypeFloat, "0.3"},
	{"Silence cap (hours)", "silence_cap_hours", TypeFloat, "24"},
	{"Topic pressure wt", "topic_pressure_weight", TypeFloat, "0.2"},
	{"Fatigue weight", "fatigue_weight", TypeFloat, "0.3"},
}

var securityDefs = []settingDef{
	{"Privacy mode", "privacy_mode", TypeBool, "true"},
	{"Kill switch", "kill_switch", TypeBool, "false"},
}
