// Command orcaster is the orchestrator CLI: it manages a workspace's tasks
// through /api/v1, authenticating with a Personal Access Token.
//
// Python->Go glossary: `package main` + `func main()` is the binary's entrypoint,
// equivalent to `if __name__ == "__main__"`. The logic lives in cmd/ (cobra).
package main

import "github.com/AInvirion/orcaster/orcaster/cmd"

func main() {
	cmd.Execute()
}
