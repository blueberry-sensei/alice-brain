"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/spinner";

export function SecureConfigTransferDialog({
  open,
  mode,
  description,
  busy,
  onOpenChange,
  onSubmit,
}: {
  open: boolean;
  mode: "export" | "import";
  description: string;
  busy: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (passphrase: string) => Promise<void>;
}) {
  const t = useTranslations("ConfigTransfer");
  const [passphrase, setPassphrase] = React.useState("");
  const [confirmation, setConfirmation] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!open) {
      setPassphrase("");
      setConfirmation("");
      setError(null);
    }
  }, [open]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (passphrase.length < 12) {
      setError(t("passphraseTooShort"));
      return;
    }
    if (mode === "export" && passphrase !== confirmation) {
      setError(t("passphraseMismatch"));
      return;
    }
    setError(null);
    await onSubmit(passphrase);
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="max-w-md">
        <form onSubmit={(event) => void submit(event)}>
          <DialogHeader>
            <DialogTitle>{t(mode === "export" ? "exportTitle" : "importTitle")}</DialogTitle>
            <DialogDescription>{description}</DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-5">
            <div className="grid gap-2">
              <Label htmlFor="portable-config-passphrase">{t("passphrase")}</Label>
              <Input
                id="portable-config-passphrase"
                type="password"
                autoComplete="new-password"
                value={passphrase}
                onChange={(event) => setPassphrase(event.target.value)}
                disabled={busy}
              />
              <p className="text-muted-foreground text-xs">{t("passphraseHint")}</p>
            </div>
            {mode === "export" && (
              <div className="grid gap-2">
                <Label htmlFor="portable-config-confirmation">{t("confirmPassphrase")}</Label>
                <Input
                  id="portable-config-confirmation"
                  type="password"
                  autoComplete="new-password"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  disabled={busy}
                />
              </div>
            )}
            {error && <p className="text-destructive text-sm">{error}</p>}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>
              {t("cancel")}
            </Button>
            <Button type="submit" disabled={busy}>
              {busy && <Spinner />}
              {t(mode === "export" ? "downloadEncrypted" : "importAndApply")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
