document.addEventListener('DOMContentLoaded', function() {
  const contactForm = document.getElementById('contactform');

  contactForm.addEventListener('submit', function(event) {
    event.preventDefault();

    const action = this.getAttribute('action');
    const messageDiv = document.getElementById('message');
    const submitButton = document.getElementById('submit');

    function displayMessage(data) {
      messageDiv.innerHTML = data;
      messageDiv.style.display = 'block'; // Ensure it's visible
      messageDiv.classList.add('show'); // Use a class for smoother animation

      const loader = document.querySelector('#contactform img.loader');
      if (loader) {
        loader.remove(); //Remove loader
      }
      submitButton.removeAttribute('disabled');

      if (data.includes('success')) {
        contactForm.style.display = 'none';
        contactForm.classList.add('hidden'); //Use a class for smoother animation
      }
    }

    messageDiv.classList.remove('show'); // Reset animation
    messageDiv.style.display = 'none';

    submitButton.insertAdjacentHTML('afterend', '<img src="images/loading.gif" class="loader" />');
    submitButton.setAttribute('disabled', 'disabled');

    const formData = new FormData(contactForm);

    fetch(action, {
      method: 'POST',
      body: formData,
    })
    .then(response => response.text())
    .then(data => {
        displayMessage(data);
    })
    .catch(error => {
      console.error('Error:', error);
      displayMessage('An error occurred. Please try again.');
    });
  });
});
