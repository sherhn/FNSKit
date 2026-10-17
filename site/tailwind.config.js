/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./*.html", "./js/**/*.js"],
  theme: {
    extend: {
      colors: {
        ink: "#171815",
        paper: "#F5F4EF",
        lime: "#C8FF32",
        violet: "#7C68FF",
        mist: "#E9E8E2"
      },
      fontFamily: {
        sans: ["Manrope", "sans-serif"]
      },
      boxShadow: {
        soft: "0 28px 80px rgba(23, 24, 21, 0.12)",
        card: "0 12px 32px rgba(23, 24, 21, 0.08)"
      }
    }
  },
  plugins: []
};
