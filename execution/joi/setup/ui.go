package main

import (
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"

	"github.com/gdamore/tcell/v2"
)

type ScreenID int

const (
	ScreenMenu ScreenID = iota
	ScreenSettings
	ScreenConvList
	ScreenConvSettings
	ScreenConfirm
	ScreenQuitConfirm
	ScreenError
	ScreenNewConv // inline input for new conversation ID
)

var (
	sNormal  = tcell.StyleDefault
	sHeader  = tcell.StyleDefault.Foreground(tcell.ColorAqua).Bold(true)
	sDirty   = tcell.StyleDefault.Foreground(tcell.ColorYellow)
	sDim     = tcell.StyleDefault.Foreground(tcell.ColorGray)
	sActive  = tcell.StyleDefault.Background(tcell.ColorNavy)
	sBoolOn  = tcell.StyleDefault.Foreground(tcell.ColorGreen)
	sBoolOff = tcell.StyleDefault.Foreground(tcell.ColorRed)
	sWarn    = tcell.StyleDefault.Foreground(tcell.ColorRed).Bold(true)
	sEdit    = tcell.StyleDefault.Background(tcell.ColorDarkBlue).Foreground(tcell.ColorWhite)
)

type App struct {
	screen     tcell.Screen
	cfg        *Config
	curScreen  ScreenID
	prevScreen ScreenID
	menuIdx    int
	settIdx    int
	settScroll int
	convIdx    int
	convScroll int
	editing    bool
	editBuf    string
	editCur    int
	areaRef    *Area
	convRef    *ConvOverride
	errMsg     string
	flash      string // brief message shown once, cleared on next draw
}

func (a *App) run() {
	a.draw()
	for {
		ev := a.screen.PollEvent()
		switch e := ev.(type) {
		case *tcell.EventKey:
			if a.handleKey(e) {
				return
			}
			a.draw()
		case *tcell.EventResize:
			a.screen.Sync()
			a.draw()
		}
	}
}

func (a *App) draw() {
	a.screen.Clear()
	switch a.curScreen {
	case ScreenMenu:
		a.drawMenu()
	case ScreenSettings:
		a.drawSettings()
	case ScreenConvList:
		a.drawConvList()
	case ScreenConvSettings:
		a.drawConvSettings()
	case ScreenConfirm:
		a.drawConfirm()
	case ScreenQuitConfirm:
		a.drawQuitConfirm()
	case ScreenError:
		a.drawError()
	case ScreenNewConv:
		a.drawNewConv()
	}
	a.screen.Show()
}

func (a *App) handleKey(ev *tcell.EventKey) bool {
	switch a.curScreen {
	case ScreenMenu:
		return a.handleMenu(ev)
	case ScreenSettings:
		return a.handleSettings(ev)
	case ScreenConvList:
		return a.handleConvList(ev)
	case ScreenConvSettings:
		return a.handleConvSettings(ev)
	case ScreenConfirm:
		return a.handleConfirm(ev)
	case ScreenQuitConfirm:
		return a.handleQuitConfirm(ev)
	case ScreenError:
		return a.handleError(ev)
	case ScreenNewConv:
		return a.handleNewConv(ev)
	}
	return false
}

// --- Menu screen ---

func (a *App) drawMenu() {
	w, h := a.screen.Size()
	Text(a.screen, 2, 1, "joi-setup", sHeader, w-4)

	items := []struct {
		name  string
		dirty bool
	}{
		{a.cfg.HW.Name, a.cfg.HW.IsDirty()},
		{a.cfg.Wind.Name, a.cfg.Wind.IsDirty()},
		{a.cfg.Security.Name, a.cfg.Security.IsDirty()},
		{"Conversations", a.convsDirty()},
	}

	for i, item := range items {
		y := 3 + i
		style := sNormal
		if i == a.menuIdx {
			Pad(a.screen, 2, y, w-4, sActive)
			style = sActive
		}
		label := item.name
		if item.dirty {
			label += " *"
		}
		prefix := "  "
		if i == a.menuIdx {
			prefix = "> "
		}
		Text(a.screen, 2, y, prefix+label, style, w-4)
	}

	// Footer
	dirty := a.cfg.TotalDirty()
	footer := "[Enter] select  [a] apply  [q] quit"
	Text(a.screen, 2, h-2, footer, sDim, w-4)
	if dirty > 0 {
		msg := fmt.Sprintf("%d pending changes", dirty)
		Text(a.screen, 2, h-3, msg, sDirty, w-4)
	}
	if a.flash != "" {
		Text(a.screen, 2, h-4, a.flash, sDirty, w-4)
		a.flash = ""
	}
}

func (a *App) handleMenu(ev *tcell.EventKey) bool {
	switch ev.Key() {
	case tcell.KeyUp:
		if a.menuIdx > 0 {
			a.menuIdx--
		}
	case tcell.KeyDown:
		if a.menuIdx < 3 {
			a.menuIdx++
		}
	case tcell.KeyEnter:
		switch a.menuIdx {
		case 0:
			a.areaRef = a.cfg.HW
			a.settIdx = 0
			a.settScroll = 0
			a.curScreen = ScreenSettings
		case 1:
			a.areaRef = a.cfg.Wind
			a.settIdx = 0
			a.settScroll = 0
			a.curScreen = ScreenSettings
		case 2:
			a.areaRef = a.cfg.Security
			a.settIdx = 0
			a.settScroll = 0
			a.curScreen = ScreenSettings
		case 3:
			a.convIdx = 0
			a.convScroll = 0
			a.curScreen = ScreenConvList
		}
	case tcell.KeyRune:
		switch ev.Rune() {
		case 'a':
			return a.tryApply()
		case 'q':
			return a.tryQuit()
		}
	}
	return false
}

// --- Settings list screen ---

func (a *App) drawSettings() {
	w, h := a.screen.Size()
	title := a.areaRef.Name
	if a.areaRef.IsDirty() {
		title += " *"
	}
	Text(a.screen, 2, 1, title, sHeader, w-4)

	visibleRows := h - 5 // header + title + footer rows
	if visibleRows < 1 {
		visibleRows = 1
	}

	labelWidth := 0
	for _, s := range a.areaRef.Settings {
		if len(s.Label) > labelWidth {
			labelWidth = len(s.Label)
		}
	}
	labelWidth += 2

	for i, s := range a.areaRef.Settings {
		if i < a.settScroll || i >= a.settScroll+visibleRows {
			continue
		}
		y := 3 + (i - a.settScroll)
		rowStyle := sNormal
		if i == a.settIdx {
			Pad(a.screen, 2, y, w-4, sActive)
			rowStyle = sActive
		}

		prefix := "  "
		if i == a.settIdx {
			prefix = "> "
		}
		Text(a.screen, 2, y, prefix+padRight(s.Label, labelWidth), rowStyle, labelWidth+2)

		valX := 4 + labelWidth

		if a.editing && i == a.settIdx {
			// Edit mode
			Text(a.screen, valX, y, a.editBuf+" ", sEdit, w-valX-2)
			// Cursor
			if a.editCur <= len([]rune(a.editBuf)) {
				cx := valX + a.editCur
				if cx < w-2 {
					r := ' '
					runes := []rune(a.editBuf)
					if a.editCur < len(runes) {
						r = runes[a.editCur]
					}
					a.screen.SetContent(cx, y, r, nil, sEdit.Reverse(true))
				}
			}
		} else {
			a.drawSettingValue(valX, y, w-valX-2, s, rowStyle)
		}
	}

	// Footer
	dirty := a.areaRef.DirtyCount()
	footer := "[Enter] edit  [r] revert  [Esc] back  [a] apply  [q] quit"
	Text(a.screen, 2, h-2, footer, sDim, w-4)
	if dirty > 0 {
		Text(a.screen, 2, h-3, fmt.Sprintf("%d pending changes", dirty), sDirty, w-4)
	}
	if a.flash != "" {
		Text(a.screen, 2, h-4, a.flash, sDirty, w-4)
		a.flash = ""
	}
}

func (a *App) drawSettingValue(x, y, maxW int, s *Setting, base tcell.Style) {
	if s.Type == TypeBool {
		if !s.Active {
			Text(a.screen, x, y, fmt.Sprintf("(default: %s)", s.Default), sDim, maxW)
		} else if s.Value == "true" {
			style := sBoolOn
			if s.IsDirty() {
				style = sDirty
			}
			text := "[ON]"
			if s.IsDirty() {
				text += fmt.Sprintf("  (was %s)", s.OnDisk)
			}
			Text(a.screen, x, y, text, style, maxW)
		} else {
			style := sBoolOff
			if s.IsDirty() {
				style = sDirty
			}
			text := "[OFF]"
			if s.IsDirty() {
				text += fmt.Sprintf("  (was %s)", s.OnDisk)
			}
			Text(a.screen, x, y, text, style, maxW)
		}
		return
	}

	if s.Deleted {
		Text(a.screen, x, y, "(will remove -> global default)", sDirty, maxW)
		return
	}

	if !s.Active {
		Text(a.screen, x, y, fmt.Sprintf("(default: %s)", s.Default), sDim, maxW)
		if s.IsDirty() {
			// Was active, now reverted to default
			extra := fmt.Sprintf("  (was %s)", s.OnDisk)
			Text(a.screen, x+len(fmt.Sprintf("(default: %s)", s.Default)), y, extra, sDim, maxW)
		}
		return
	}

	if s.IsDirty() {
		was := s.OnDisk
		if !s.OnDiskActive {
			was = "default"
		}
		text := fmt.Sprintf("%s  (was %s)", s.Value, was)
		Text(a.screen, x, y, text, sDirty, maxW)
	} else {
		Text(a.screen, x, y, s.Value, base, maxW)
	}
}

func (a *App) handleSettings(ev *tcell.EventKey) bool {
	if a.editing {
		return a.handleInlineEdit(ev)
	}

	_, h := a.screen.Size()
	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}
	maxIdx := len(a.areaRef.Settings) - 1

	switch ev.Key() {
	case tcell.KeyUp:
		if a.settIdx > 0 {
			a.settIdx--
			if a.settIdx < a.settScroll {
				a.settScroll = a.settIdx
			}
		}
	case tcell.KeyDown:
		if a.settIdx < maxIdx {
			a.settIdx++
			if a.settIdx >= a.settScroll+visibleRows {
				a.settScroll = a.settIdx - visibleRows + 1
			}
		}
	case tcell.KeyEnter:
		s := a.areaRef.Settings[a.settIdx]
		if s.Type == TypeBool {
			a.toggleBool(s)
		} else {
			a.startEdit(s)
		}
	case tcell.KeyEscape:
		a.curScreen = ScreenMenu
	case tcell.KeyRune:
		switch ev.Rune() {
		case ' ':
			s := a.areaRef.Settings[a.settIdx]
			if s.Type == TypeBool {
				a.toggleBool(s)
			}
		case 'r':
			s := a.areaRef.Settings[a.settIdx]
			s.Value = s.OnDisk
			s.Active = s.OnDiskActive
			s.Deleted = false
		case 'a':
			return a.tryApply()
		case 'q':
			return a.tryQuit()
		}
	}
	return false
}

func (a *App) toggleBool(s *Setting) {
	if !s.Active {
		s.Active = true
		if s.Default == "true" {
			s.Value = "false"
		} else {
			s.Value = "true"
		}
	} else {
		if s.Value == "true" {
			s.Value = "false"
		} else {
			s.Value = "true"
		}
	}
}

func (a *App) startEdit(s *Setting) {
	a.editing = true
	if s.Active {
		a.editBuf = s.Value
	} else {
		a.editBuf = s.Default
	}
	a.editCur = len([]rune(a.editBuf))
}

func (a *App) handleInlineEdit(ev *tcell.EventKey) bool {
	runes := []rune(a.editBuf)

	switch ev.Key() {
	case tcell.KeyEnter:
		s := a.areaRef.Settings[a.settIdx]
		if err := validateValue(a.editBuf, s.Type); err != nil {
			a.flash = err.Error()
			return false
		}
		s.Value = a.editBuf
		s.Active = true
		a.editing = false
	case tcell.KeyEscape:
		a.editing = false
	case tcell.KeyBackspace, tcell.KeyBackspace2:
		if a.editCur > 0 {
			runes = append(runes[:a.editCur-1], runes[a.editCur:]...)
			a.editBuf = string(runes)
			a.editCur--
		}
	case tcell.KeyDelete:
		if a.editCur < len(runes) {
			runes = append(runes[:a.editCur], runes[a.editCur+1:]...)
			a.editBuf = string(runes)
		}
	case tcell.KeyLeft:
		if a.editCur > 0 {
			a.editCur--
		}
	case tcell.KeyRight:
		if a.editCur < len(runes) {
			a.editCur++
		}
	case tcell.KeyHome:
		a.editCur = 0
	case tcell.KeyEnd:
		a.editCur = len(runes)
	case tcell.KeyRune:
		r := ev.Rune()
		runes = append(runes[:a.editCur], append([]rune{r}, runes[a.editCur:]...)...)
		a.editBuf = string(runes)
		a.editCur++
	}
	return false
}

func validateValue(val string, typ ValType) error {
	switch typ {
	case TypeInt:
		if _, err := strconv.Atoi(val); err != nil {
			return fmt.Errorf("invalid integer: %s", val)
		}
	case TypeFloat:
		if _, err := strconv.ParseFloat(val, 64); err != nil {
			return fmt.Errorf("invalid number: %s", val)
		}
	case TypeString:
		if val == "" {
			return fmt.Errorf("value cannot be empty")
		}
	}
	return nil
}

// --- Conversation list screen ---

func (a *App) drawConvList() {
	w, h := a.screen.Size()
	Text(a.screen, 2, 1, "Conversations", sHeader, w-4)

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}

	totalItems := len(a.cfg.Convos) + 1 // +1 for "+ New..."

	for i := 0; i < totalItems; i++ {
		if i < a.convScroll || i >= a.convScroll+visibleRows {
			continue
		}
		y := 3 + (i - a.convScroll)
		rowStyle := sNormal
		if i == a.convIdx {
			Pad(a.screen, 2, y, w-4, sActive)
			rowStyle = sActive
		}

		prefix := "  "
		if i == a.convIdx {
			prefix = "> "
		}

		if i < len(a.cfg.Convos) {
			c := a.cfg.Convos[i]
			label := c.ID
			if c.Scope == "groups" {
				label = "GRP:" + label
			}
			if c.IsDirty() {
				label += " *"
			}
			tags := c.Tags()
			tagStyle := sDim
			if tags != "no overrides" {
				tagStyle = sBoolOn
			}
			if i == a.convIdx {
				tagStyle = tagStyle.Background(tcell.ColorNavy)
			}
			Text(a.screen, 2, y, prefix+label, rowStyle, w-4)
			Text(a.screen, 4+len(prefix)+len(label)+2, y, tags, tagStyle, w-4)
		} else {
			Text(a.screen, 2, y, prefix+"+ New...", rowStyle, w-4)
		}
	}

	footer := "[Enter] edit  [d] delete overrides  [Esc] back  [a] apply  [q] quit"
	Text(a.screen, 2, h-2, footer, sDim, w-4)
	if a.flash != "" {
		Text(a.screen, 2, h-3, a.flash, sDirty, w-4)
		a.flash = ""
	}
}

func (a *App) handleConvList(ev *tcell.EventKey) bool {
	_, h := a.screen.Size()
	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}
	totalItems := len(a.cfg.Convos) + 1
	maxIdx := totalItems - 1

	switch ev.Key() {
	case tcell.KeyUp:
		if a.convIdx > 0 {
			a.convIdx--
			if a.convIdx < a.convScroll {
				a.convScroll = a.convIdx
			}
		}
	case tcell.KeyDown:
		if a.convIdx < maxIdx {
			a.convIdx++
			if a.convIdx >= a.convScroll+visibleRows {
				a.convScroll = a.convIdx - visibleRows + 1
			}
		}
	case tcell.KeyEnter:
		if a.convIdx < len(a.cfg.Convos) {
			a.convRef = a.cfg.Convos[a.convIdx]
			a.settIdx = 0
			a.curScreen = ScreenConvSettings
		} else {
			// "+ New..."
			a.editBuf = ""
			a.editCur = 0
			a.curScreen = ScreenNewConv
		}
	case tcell.KeyEscape:
		a.curScreen = ScreenMenu
	case tcell.KeyRune:
		switch ev.Rune() {
		case 'd':
			if a.convIdx < len(a.cfg.Convos) {
				c := a.cfg.Convos[a.convIdx]
				if c.IsNew {
					// Remove from list entirely
					a.cfg.Convos = append(a.cfg.Convos[:a.convIdx], a.cfg.Convos[a.convIdx+1:]...)
					if a.convIdx >= len(a.cfg.Convos)+1 {
						a.convIdx = len(a.cfg.Convos)
					}
				} else {
					for _, s := range c.AllSettings() {
						if s != nil {
							s.Deleted = true
						}
					}
				}
			}
		case 'a':
			return a.tryApply()
		case 'q':
			return a.tryQuit()
		}
	}
	return false
}

// --- New conversation input screen ---

func (a *App) drawNewConv() {
	w, h := a.screen.Size()
	Text(a.screen, 2, 1, "New Conversation", sHeader, w-4)
	Text(a.screen, 2, 3, "Enter conversation ID (phone number or group ID):", sNormal, w-4)

	// Input field
	Text(a.screen, 2, 5, a.editBuf+" ", sEdit, w-4)
	if a.editCur <= len([]rune(a.editBuf)) {
		cx := 2 + a.editCur
		if cx < w-2 {
			r := ' '
			runes := []rune(a.editBuf)
			if a.editCur < len(runes) {
				r = runes[a.editCur]
			}
			a.screen.SetContent(cx, 5, r, nil, sEdit.Reverse(true))
		}
	}

	Text(a.screen, 2, 7, "Phone numbers start with + (e.g., +1234567890)", sDim, w-4)
	Text(a.screen, 2, h-2, "[Enter] create  [Esc] cancel", sDim, w-4)
	if a.flash != "" {
		Text(a.screen, 2, h-3, a.flash, sDirty, w-4)
		a.flash = ""
	}
}

func (a *App) handleNewConv(ev *tcell.EventKey) bool {
	runes := []rune(a.editBuf)

	switch ev.Key() {
	case tcell.KeyEnter:
		raw := strings.TrimSpace(a.editBuf)
		if raw == "" {
			a.flash = "ID cannot be empty"
			return false
		}
		sanitized := sanitizeScope(raw)
		if sanitized == "" {
			a.flash = "Invalid ID"
			return false
		}
		// Determine scope: phone numbers start with + before sanitize
		scope := "groups"
		if strings.HasPrefix(raw, "+") {
			scope = "users"
		}
		// Duplicate check
		for i, c := range a.cfg.Convos {
			if c.ID == sanitized && c.Scope == scope {
				a.convRef = c
				a.convIdx = i
				a.settIdx = 0
				a.curScreen = ScreenConvSettings
				return false
			}
		}
		conv := &ConvOverride{ID: sanitized, Scope: scope, IsNew: true}
		a.cfg.Convos = append(a.cfg.Convos, conv)
		a.convRef = conv
		a.convIdx = len(a.cfg.Convos) - 1
		a.settIdx = 0
		a.curScreen = ScreenConvSettings
	case tcell.KeyEscape:
		a.curScreen = ScreenConvList
	case tcell.KeyBackspace, tcell.KeyBackspace2:
		if a.editCur > 0 {
			runes = append(runes[:a.editCur-1], runes[a.editCur:]...)
			a.editBuf = string(runes)
			a.editCur--
		}
	case tcell.KeyDelete:
		if a.editCur < len(runes) {
			runes = append(runes[:a.editCur], runes[a.editCur+1:]...)
			a.editBuf = string(runes)
		}
	case tcell.KeyLeft:
		if a.editCur > 0 {
			a.editCur--
		}
	case tcell.KeyRight:
		if a.editCur < len(runes) {
			a.editCur++
		}
	case tcell.KeyHome:
		a.editCur = 0
	case tcell.KeyEnd:
		a.editCur = len(runes)
	case tcell.KeyRune:
		r := ev.Rune()
		runes = append(runes[:a.editCur], append([]rune{r}, runes[a.editCur:]...)...)
		a.editBuf = string(runes)
		a.editCur++
	}
	return false
}

// --- Per-conversation settings screen ---

type convSettingRef struct {
	label    string
	setting  **Setting
	ext      string
	typ      ValType
	defLabel string
}

func (a *App) convSettingRefs() []convSettingRef {
	return []convSettingRef{
		{"Model", &a.convRef.Model, ".model", TypeString, "global default"},
		{"Context msgs", &a.convRef.Context, ".context", TypeInt, "global default"},
		{"Compact batch", &a.convRef.Compact, ".compact_window", TypeInt, "global default"},
		{"System prompt", &a.convRef.Prompt, ".txt", TypeText, "default.txt"},
	}
}

func (a *App) drawConvSettings() {
	w, h := a.screen.Size()
	title := a.convRef.ID
	if a.convRef.Scope == "groups" {
		title = "GRP:" + title
	}
	if a.convRef.IsDirty() {
		title += " *"
	}
	Text(a.screen, 2, 1, title, sHeader, w-4)

	refs := a.convSettingRefs()
	labelWidth := 16

	for i, ref := range refs {
		y := 3 + i
		rowStyle := sNormal
		if i == a.settIdx {
			Pad(a.screen, 2, y, w-4, sActive)
			rowStyle = sActive
		}

		prefix := "  "
		if i == a.settIdx {
			prefix = "> "
		}
		Text(a.screen, 2, y, prefix+padRight(ref.label, labelWidth), rowStyle, labelWidth+2)

		valX := 4 + labelWidth
		s := *ref.setting

		if a.editing && i == a.settIdx && ref.typ != TypeText {
			Text(a.screen, valX, y, a.editBuf+" ", sEdit, w-valX-2)
			if a.editCur <= len([]rune(a.editBuf)) {
				cx := valX + a.editCur
				if cx < w-2 {
					r := ' '
					runes := []rune(a.editBuf)
					if a.editCur < len(runes) {
						r = runes[a.editCur]
					}
					a.screen.SetContent(cx, y, r, nil, sEdit.Reverse(true))
				}
			}
		} else if s == nil {
			Text(a.screen, valX, y, fmt.Sprintf("(%s)", ref.defLabel), sDim, w-valX-2)
		} else if s.Deleted {
			Text(a.screen, valX, y, "(will remove -> global default)", sDirty, w-valX-2)
		} else if ref.typ == TypeText {
			lines := strings.Count(s.Value, "\n") + 1
			chars := len(s.Value)
			if chars == 0 {
				Text(a.screen, valX, y, "(empty)", sDim, w-valX-2)
			} else {
				style := sNormal
				if s.IsDirty() {
					style = sDirty
				}
				if i == a.settIdx {
					style = style.Background(tcell.ColorNavy)
				}
				Text(a.screen, valX, y, fmt.Sprintf("custom (%d lines, %d chars)", lines, chars), style, w-valX-2)
			}
		} else if s.IsDirty() {
			was := s.OnDisk
			if was == "" {
				was = "new"
			}
			Text(a.screen, valX, y, fmt.Sprintf("%s  (was %s)", s.Value, was), sDirty, w-valX-2)
		} else {
			Text(a.screen, valX, y, s.Value, rowStyle, w-valX-2)
		}
	}

	Text(a.screen, 2, 3+len(refs)+1, "Values without a file fall back to global defaults.", sDim, w-4)
	footer := "[Enter] edit  [x] clear  [r] revert  [Esc] back  [a] apply  [q] quit"
	Text(a.screen, 2, h-2, footer, sDim, w-4)
	if a.flash != "" {
		Text(a.screen, 2, h-3, a.flash, sDirty, w-4)
		a.flash = ""
	}
}

func (a *App) handleConvSettings(ev *tcell.EventKey) bool {
	if a.editing {
		return a.handleConvInlineEdit(ev)
	}

	refs := a.convSettingRefs()
	maxIdx := len(refs) - 1

	switch ev.Key() {
	case tcell.KeyUp:
		if a.settIdx > 0 {
			a.settIdx--
		}
	case tcell.KeyDown:
		if a.settIdx < maxIdx {
			a.settIdx++
		}
	case tcell.KeyEnter:
		ref := refs[a.settIdx]
		if ref.typ == TypeText {
			a.editPromptExternal()
		} else {
			s := *ref.setting
			if s == nil {
				// Create new setting
				s = &Setting{
					Label: ref.label,
					Key:   ref.ext,
					Type:  ref.typ,
				}
				*ref.setting = s
			}
			a.editing = true
			if s.Active && s.Value != "" {
				a.editBuf = s.Value
			} else {
				a.editBuf = ""
			}
			a.editCur = len([]rune(a.editBuf))
		}
	case tcell.KeyEscape:
		a.curScreen = ScreenConvList
	case tcell.KeyRune:
		switch ev.Rune() {
		case 'x':
			ref := refs[a.settIdx]
			s := *ref.setting
			if s != nil {
				if s.OnDisk == "" && !s.OnDiskActive {
					// Newly created, not on disk: set pointer to nil
					*ref.setting = nil
				} else {
					s.Deleted = true
				}
			}
		case 'r':
			ref := refs[a.settIdx]
			s := *ref.setting
			if s != nil {
				if s.OnDisk == "" && !s.OnDiskActive {
					// Was newly created: remove entirely
					*ref.setting = nil
				} else {
					s.Value = s.OnDisk
					s.Active = s.OnDiskActive
					s.Deleted = false
				}
			}
		case 'a':
			return a.tryApply()
		case 'q':
			return a.tryQuit()
		}
	}
	return false
}

func (a *App) handleConvInlineEdit(ev *tcell.EventKey) bool {
	runes := []rune(a.editBuf)
	ref := a.convSettingRefs()[a.settIdx]

	switch ev.Key() {
	case tcell.KeyEnter:
		s := *ref.setting
		if err := validateValue(a.editBuf, ref.typ); err != nil {
			a.flash = err.Error()
			return false
		}
		s.Value = a.editBuf
		s.Active = true
		a.editing = false
	case tcell.KeyEscape:
		// Cancel: if this was a brand new setting with no on-disk, remove it
		s := *ref.setting
		if s != nil && s.OnDisk == "" && !s.OnDiskActive && s.Value == "" {
			*ref.setting = nil
		}
		a.editing = false
	case tcell.KeyBackspace, tcell.KeyBackspace2:
		if a.editCur > 0 {
			runes = append(runes[:a.editCur-1], runes[a.editCur:]...)
			a.editBuf = string(runes)
			a.editCur--
		}
	case tcell.KeyDelete:
		if a.editCur < len(runes) {
			runes = append(runes[:a.editCur], runes[a.editCur+1:]...)
			a.editBuf = string(runes)
		}
	case tcell.KeyLeft:
		if a.editCur > 0 {
			a.editCur--
		}
	case tcell.KeyRight:
		if a.editCur < len(runes) {
			a.editCur++
		}
	case tcell.KeyHome:
		a.editCur = 0
	case tcell.KeyEnd:
		a.editCur = len(runes)
	case tcell.KeyRune:
		r := ev.Rune()
		runes = append(runes[:a.editCur], append([]rune{r}, runes[a.editCur:]...)...)
		a.editBuf = string(runes)
		a.editCur++
	}
	return false
}

func (a *App) editPromptExternal() {
	ref := a.convSettingRefs()[a.settIdx]
	s := *ref.setting

	// Create setting if nil
	if s == nil {
		s = &Setting{
			Label: ref.label,
			Key:   ref.ext,
			Type:  TypeText,
		}
		*ref.setting = s
	}

	// Write current content to temp file
	tmp, err := os.CreateTemp("", "joi-prompt-*.txt")
	if err != nil {
		a.flash = fmt.Sprintf("temp file: %v", err)
		return
	}
	tmpPath := tmp.Name()
	tmp.WriteString(s.Value)
	tmp.Close()

	// Release terminal
	a.screen.Fini()

	// Determine editor
	editor := os.Getenv("EDITOR")
	if editor == "" {
		editor = "vi"
	}

	cmd := exec.Command(editor, tmpPath)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Run()

	// Read back
	data, err := os.ReadFile(tmpPath)
	if err == nil {
		s.Value = string(data)
		if len(strings.TrimSpace(s.Value)) > 0 {
			s.Active = true
		}
	}
	os.Remove(tmpPath)

	// Re-init screen
	scr, err := tcell.NewScreen()
	if err != nil {
		fmt.Fprintf(os.Stderr, "joi-setup: screen re-init failed: %v\n", err)
		os.Exit(1)
	}
	if err := scr.Init(); err != nil {
		fmt.Fprintf(os.Stderr, "joi-setup: screen re-init failed: %v\n", err)
		os.Exit(1)
	}
	scr.SetStyle(sNormal)
	a.screen = scr
}

// --- Confirm screen ---

func (a *App) drawConfirm() {
	w, h := a.screen.Size()
	Text(a.screen, 2, 1, "Apply Changes", sWarn, w-4)

	y := 3
	Text(a.screen, 2, y, "This will:", sNormal, w-4)
	y++
	Text(a.screen, 4, y, "1. Stop joi-api service", sNormal, w-6)
	y++
	dirty := a.cfg.TotalDirty()
	Text(a.screen, 4, y, fmt.Sprintf("2. Write %d changes to disk", dirty), sNormal, w-6)
	y++
	Text(a.screen, 4, y, "3. Exit joi-setup", sNormal, w-6)
	y += 2

	Text(a.screen, 2, y, "Changes:", sNormal, w-4)
	y++
	for _, change := range a.cfg.DirtyChanges() {
		Text(a.screen, 4, y, change, sDirty, w-6)
		y++
		if y >= h-5 {
			Text(a.screen, 4, y, "... (more)", sDim, w-6)
			break
		}
	}

	Text(a.screen, 2, h-4, "Service will NOT restart automatically.", sWarn, w-4)
	Text(a.screen, 2, h-2, "[y] confirm  [n] cancel", sDim, w-4)
}

func (a *App) handleConfirm(ev *tcell.EventKey) bool {
	if ev.Key() == tcell.KeyRune {
		switch ev.Rune() {
		case 'y':
			if err := a.cfg.Apply(); err != nil {
				a.errMsg = err.Error()
				a.curScreen = ScreenError
				return false
			}
			return true // exit
		case 'n':
			a.curScreen = a.prevScreen
		}
	}
	if ev.Key() == tcell.KeyEscape {
		a.curScreen = a.prevScreen
	}
	return false
}

// --- Quit confirm screen ---

func (a *App) drawQuitConfirm() {
	w, h := a.screen.Size()
	dirty := a.cfg.TotalDirty()
	Text(a.screen, 2, 3, fmt.Sprintf("%d unsaved changes will be lost. Quit?", dirty), sWarn, w-4)
	Text(a.screen, 2, h-2, "[y] quit  [n] cancel", sDim, w-4)
}

func (a *App) handleQuitConfirm(ev *tcell.EventKey) bool {
	if ev.Key() == tcell.KeyRune {
		switch ev.Rune() {
		case 'y':
			return true
		case 'n':
			a.curScreen = a.prevScreen
		}
	}
	if ev.Key() == tcell.KeyEscape {
		a.curScreen = a.prevScreen
	}
	return false
}

// --- Error screen ---

func (a *App) drawError() {
	w, h := a.screen.Size()
	Text(a.screen, 2, 1, "Apply Failed", sWarn, w-4)
	Text(a.screen, 2, 3, "Service may have been stopped.", sNormal, w-4)
	Text(a.screen, 2, 5, a.errMsg, sWarn, w-4)
	Text(a.screen, 2, 7, "Backups were created before any writes.", sNormal, w-4)
	Text(a.screen, 2, h-2, "[Enter/Esc] return  [q] exit", sDim, w-4)
}

func (a *App) handleError(ev *tcell.EventKey) bool {
	switch ev.Key() {
	case tcell.KeyEnter, tcell.KeyEscape:
		a.curScreen = a.prevScreen
	case tcell.KeyRune:
		if ev.Rune() == 'q' {
			return true
		}
	}
	return false
}

// --- Helpers ---

func (a *App) tryApply() bool {
	if a.cfg.TotalDirty() == 0 {
		a.flash = "No changes to apply"
		return false
	}
	a.prevScreen = a.curScreen
	a.curScreen = ScreenConfirm
	return false
}

func (a *App) tryQuit() bool {
	if a.cfg.TotalDirty() == 0 {
		return true
	}
	a.prevScreen = a.curScreen
	a.curScreen = ScreenQuitConfirm
	return false
}

func (a *App) convsDirty() bool {
	for _, c := range a.cfg.Convos {
		if c.IsDirty() {
			return true
		}
	}
	return false
}

func padRight(s string, width int) string {
	if len(s) >= width {
		return s
	}
	return s + strings.Repeat(" ", width-len(s))
}
