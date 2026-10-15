# Model Management

Runtime services read small active manifests from `model_store/active`.
Large model files live under `model_store/registry` and are ignored by Git.

The per-character `models.json` files are the source of truth for the desktop
selector, character editor, and training preview. Activation records
`previous_model_id` and an activation history. The desktop validates a new
version by starting and stopping its runtime service before committing the
active manifest. Manifest writes use temporary files and restore the previous
state if a write fails. A failed service start also tries to restore the
previous version.

The newest five trained versions remain available by default. The active,
previous, and pinned versions are protected. Registration does not silently
delete files: the desktop shows the exact cleanup list and estimated space
before pruning. Checkpoints remain in the training output directory.

Character bundles include character resources and both model manifests.
The default package contains model references only. Choosing "include model
files" copies the referenced artifacts into the bundle; importing that bundle
installs them into the destination registry.
