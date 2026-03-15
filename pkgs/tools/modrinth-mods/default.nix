{
  python3,
  lib,
}:

python3.pkgs.buildPythonApplication {
  pname = "modrinth-mods";
  version = "0.1.0";
  format = "other";

  src = ./.;

  dontBuild = true;

  installPhase = ''
    install -Dm755 modrinth-mods.py $out/bin/modrinth-mods
  '';

  meta = with lib; {
    description = "Manage Modrinth mods via a manifest and Nix-compatible lock file";
    mainProgram = "modrinth-mods";
  };
}
