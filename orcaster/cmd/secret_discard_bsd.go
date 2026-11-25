//go:build darwin || freebsd

package cmd

import "golang.org/x/sys/unix"

// flushTerminalInput discards what the terminal has read but not yet consumed.
// On BSD, tcflush(fd, TCIFLUSH) is ioctl(TIOCFLUSH) with a pointer to FREAD,
// which has the same value as TCIFLUSH.
func flushTerminalInput(fd int) error {
	return unix.IoctlSetPointerInt(fd, unix.TIOCFLUSH, unix.TCIFLUSH)
}
