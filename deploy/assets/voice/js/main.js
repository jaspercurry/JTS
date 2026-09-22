// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { wireConfirmForms } from "/assets/shared/js/confirm-forms.js";
import { wireCopyButtons } from "/assets/shared/js/copy.js";

wireConfirmForms();
wireCopyButtons();

const providerForm = document.getElementById("provider-form");
providerForm.addEventListener("change", () => providerForm.requestSubmit());
document.getElementById("choose-provider").hidden = true;
