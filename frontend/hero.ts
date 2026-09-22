import { heroui } from "@heroui/react"

export default heroui({
  layout: {
    radius: {
      small: "8px",
      medium: "12px",
      large: "16px",
    },
  },
  themes: {
    light: {
      colors: {
        primary: {
          DEFAULT: "#d8903d",
          foreground: "#171411",
        },
      },
    },
  },
})
