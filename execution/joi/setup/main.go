package main

import (
	"fmt"
	"os"

	"github.com/gdamore/tcell/v2"
)

func main() {
	if os.Getuid() != 0 {
		fmt.Fprintln(os.Stderr, "joi-setup: must run as root (use sudo)")
		os.Exit(1)
	}

	cfg, err := LoadAll()
	if err != nil {
		fmt.Fprintf(os.Stderr, "joi-setup: %v\n", err)
		os.Exit(1)
	}

	s, err := tcell.NewScreen()
	if err != nil {
		fmt.Fprintf(os.Stderr, "joi-setup: screen: %v\n", err)
		os.Exit(1)
	}
	if err := s.Init(); err != nil {
		fmt.Fprintf(os.Stderr, "joi-setup: screen: %v\n", err)
		os.Exit(1)
	}
	s.SetStyle(sNormal)

	app := &App{screen: s, cfg: cfg, curScreen: ScreenMenu}
	app.run()
	app.screen.Fini()
	fmt.Println()
}
