fetch("/api/catalog").then(response => response.json()).then(catalog => {
  for (const agent of catalog.agents) {
    const article = document.createElement("article");
    const title = document.createElement("h2"); title.textContent = agent.id;
    const description = document.createElement("p"); description.textContent = agent.description;
    article.append(title, description); document.querySelector("#agents").append(article);
  }
});
