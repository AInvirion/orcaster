//go:build linux

package cmd

import "golang.org/x/sys/unix"

// flushTerminalInput discards what the terminal has read but not yet consumed
// (tcflush(fd, TCIFLUSH)).
func flushTerminalInput(fd int) error {
	return unix.IoctlSetInt(fd, unix.TCFLSH, unix.TCIFLUSH)
}
