module.exports.runtime = {
  handler: async function ({ message }) {
    try {
      const response = await fetch("http://127.0.0.1:8000/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: message })
      });
      const data = await response.json();
      return data.response;
    } catch (e) {
      return `Axoworks Engine Error: ${e.message}`;
    }
  }
};