"Miscellaneous Nix dependencies"
import rsyscall.nix as nix

bash_nixdep = nix.import_nix_dep("bash")
coreutils_nixdep = nix.import_nix_dep("coreutils")
hello_nixdep = nix.import_nix_dep("hello")
