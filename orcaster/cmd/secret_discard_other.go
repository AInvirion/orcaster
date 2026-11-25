//go:build !linux && !darwin && !freebsd

package cmd

import "errors"

// flushTerminalInput is not implemented outside Linux, macOS and FreeBSD; the
// error makes the message warn about the rest of the pasted text.
func flushTerminalInput(int) error {
	return errors.New("discarding terminal input is not supported on this platform")
}
