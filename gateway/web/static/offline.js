// Page hors connexion dans l'application Android (android/) : l'adresse du PC a pu changer, on permet d'en saisir
// une autre sans attendre que la gateway réponde.
const button = document.getElementById("change-server");
if (window.LezardApp?.changeServer) {
  button.hidden = false;
  button.addEventListener("click", () => window.LezardApp.changeServer());
}
